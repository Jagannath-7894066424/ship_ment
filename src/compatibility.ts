import type { PrismaClient } from "../generated/prisma/client.js";

/**
 * Compatibility resolution for two cargoes (46 CFR Part 150).
 *
 * Each source has its own cargo_chemical row, so both cargoes are first expanded to
 * every row of the same chemical with the DB function cargo_identity(id):
 *   strict = same normalized name, loose = one non-ambiguous synonym hop.
 *
 * Then, most specific rule first:
 *
 *   1. Chemical-pair exception between the two identities (either stored order).
 *   2. Cargo->group exception: one cargo against a reactive group of the other.
 *   3. Reactive-group matrix (compatibility) for the cargoes' groups.
 *
 * Within a step the most restrictive result wins. Loose (synonym) matches may only
 * make a result incompatible; a "compatible" exception must match strictly.
 * Group 0 ("Unassigned Cargoes") has no matrix entries and is never "same group =>
 * compatible": an unassigned cargo is decided only by an exception, else unknown.
 */

export type CompatibilitySource = "exception" | "matrix" | "unknown";

export interface CompatibilityResult {
  /** true = compatible, false = incompatible, null = undetermined (no data). */
  compatible: boolean | null;
  source: CompatibilitySource;
  detail: Record<string, unknown>;
}

const UNASSIGNED_CODE = 0;

interface Identity {
  strict: Set<number>;
  all: number[];
}

interface ExceptionRow {
  id: number;
  cargo_a_id: number;
  cargo_b_id: number | null;
  group_b_id: number | null;
  compatible: boolean;
  exception_type: string;
}

function canonical(a: number, b: number): [number, number] {
  return a <= b ? [a, b] : [b, a];
}

async function identity(prisma: PrismaClient, cargoId: number): Promise<Identity> {
  const rows = await prisma.$queryRaw<{ cargo_id: number; strict: boolean }[]>`
    SELECT cargo_id, strict FROM cargo_identity(${cargoId}::int)`;
  return {
    strict: new Set(rows.filter((r) => r.strict).map((r) => r.cargo_id)),
    all: rows.map((r) => r.cargo_id),
  };
}

/** Incompatible (strict or loose) beats compatible; compatible must be strict. */
function pick(
  rows: ExceptionRow[],
  isStrict: (r: ExceptionRow) => boolean,
  kind: "pair" | "cargo_group",
): CompatibilityResult | null {
  for (const want of [false, true]) {
    const hits = rows.filter((r) => r.compatible === want && (!want || isStrict(r)));
    if (hits.length === 0) continue;
    const e = hits.find(isStrict) ?? hits[0]!;
    return {
      compatible: e.compatible,
      source: "exception",
      detail: {
        kind,
        exception_id: e.id,
        match: isStrict(e) ? "exact" : "synonym",
        group_id: e.group_b_id,
        exception_type: e.exception_type,
      },
    };
  }
  return null;
}

export async function resolveCompatibility(
  prisma: PrismaClient,
  cargoAId: number,
  cargoBId: number,
): Promise<CompatibilityResult> {
  const [idA, idB] = await Promise.all([identity(prisma, cargoAId), identity(prisma, cargoBId)]);

  // 1) Chemical-pair exception between the identities, either order.
  const pairRows = await prisma.compatibility_exception.findMany({
    where: {
      OR: [
        { cargo_a_id: { in: idA.all }, cargo_b_id: { in: idB.all } },
        { cargo_a_id: { in: idB.all }, cargo_b_id: { in: idA.all } },
      ],
    },
  });
  const pairHit = pick(
    pairRows,
    (r) =>
      (idA.strict.has(r.cargo_a_id) && idB.strict.has(r.cargo_b_id!)) ||
      (idB.strict.has(r.cargo_a_id) && idA.strict.has(r.cargo_b_id!)),
    "pair",
  );
  if (pairHit) return pairHit;

  const groupsOf = (ids: Set<number>) =>
    prisma.cargo_reactive_group.findMany({
      where: { cargo_id: { in: [...ids] } },
      select: { reactive_group_id: true, group_code: true },
      distinct: ["reactive_group_id"],
    });
  const [groupsA, groupsB] = await Promise.all([groupsOf(idA.strict), groupsOf(idB.strict)]);
  const gidsA = new Set(groupsA.map((g) => g.reactive_group_id));
  const gidsB = new Set(groupsB.map((g) => g.reactive_group_id));

  // 2) Cargo->group exception: A against B's groups, or B against A's groups.
  const groupRows = await prisma.compatibility_exception.findMany({
    where: {
      cargo_b_id: null,
      OR: [
        { cargo_a_id: { in: idA.all }, group_b_id: { in: [...gidsB] } },
        { cargo_a_id: { in: idB.all }, group_b_id: { in: [...gidsA] } },
      ],
    },
  });
  const groupHit = pick(
    groupRows,
    (r) =>
      (idA.strict.has(r.cargo_a_id) && gidsB.has(r.group_b_id!)) ||
      (idB.strict.has(r.cargo_a_id) && gidsA.has(r.group_b_id!)),
    "cargo_group",
  );
  if (groupHit) return groupHit;

  if (gidsA.size === 0 || gidsB.size === 0) {
    return {
      compatible: null,
      source: "unknown",
      detail: { reason: "one or both cargoes have no reactive group" },
    };
  }

  // 3) Matrix. Unassigned (group 0) cargoes have no chart entry.
  const assignedA = groupsA.filter((g) => g.group_code !== UNASSIGNED_CODE).map((g) => g.reactive_group_id);
  const assignedB = groupsB.filter((g) => g.group_code !== UNASSIGNED_CODE).map((g) => g.reactive_group_id);
  if (assignedA.length === 0 || assignedB.length === 0) {
    return {
      compatible: null,
      source: "unknown",
      detail: {
        reason:
          "unassigned cargo (group 0): no chart entry and no Appendix I exception; decide case by case",
      },
    };
  }

  let matched = false;
  let incompatibleHit:
    | { group_a_id: number; group_b_id: number; reaction_description: string | null }
    | null = null;

  for (const x of assignedA) {
    for (const y of assignedB) {
      if (x === y) {
        matched = true; // a group is compatible with itself
        continue;
      }
      const [ga, gb] = canonical(x, y);
      const row = await prisma.compatibility.findUnique({
        where: { group_a_id_group_b_id: { group_a_id: ga, group_b_id: gb } },
      });
      if (!row) continue;
      matched = true;
      if (row.compatible === false) {
        incompatibleHit = {
          group_a_id: ga,
          group_b_id: gb,
          reaction_description: row.reaction_description,
        };
      }
    }
  }

  if (incompatibleHit) {
    return { compatible: false, source: "matrix", detail: incompatibleHit };
  }
  if (matched) {
    return { compatible: true, source: "matrix", detail: {} };
  }
  return {
    compatible: null,
    source: "unknown",
    detail: { reason: "no matrix entry for the cargoes' reactive groups" },
  };
}
