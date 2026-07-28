import type { PrismaClient } from "../generated/prisma/client.js";

/**
 * Compatibility resolution for two cargoes (46 CFR Part 150).
 *
 * The lookup ALWAYS follows this order — an Appendix I exception overrides the
 * reactive-group matrix:
 *
 *   1. Search compatibility_exception using (cargo_a_id, cargo_b_id).
 *   2. If an exception exists, return that result immediately.
 *   3. Otherwise, determine the reactive groups of both cargoes.
 *   4. Query the compatibility matrix for those groups.
 *   5. Return the matrix result.
 *
 * Both `compatibility` and `compatibility_exception` store each pair once in
 * canonical order (smaller id first), so every lookup canonicalises the pair.
 */

export type CompatibilitySource = "exception" | "matrix" | "unknown";

export interface CompatibilityResult {
  /** true = compatible, false = incompatible, null = undetermined (no data). */
  compatible: boolean | null;
  source: CompatibilitySource;
  detail: Record<string, unknown>;
}

/** Return the pair in canonical order (smaller id first). */
function canonical(a: number, b: number): [number, number] {
  return a <= b ? [a, b] : [b, a];
}

export async function resolveCompatibility(
  prisma: PrismaClient,
  cargoAId: number,
  cargoBId: number,
): Promise<CompatibilityResult> {
  const [a, b] = canonical(cargoAId, cargoBId);

  // 1–2) Exception overrides everything.
  const exception = await prisma.compatibility_exception.findUnique({
    where: { cargo_a_id_cargo_b_id: { cargo_a_id: a, cargo_b_id: b } },
  });
  if (exception) {
    return {
      compatible: exception.compatible,
      source: "exception",
      detail: {
        exception_type: exception.exception_type,
        appendix: exception.appendix,
        section: exception.section,
        reason: exception.reason,
      },
    };
  }

  // 3) Reactive groups of both cargoes.
  const [groupsARows, groupsBRows] = await Promise.all([
    prisma.cargo_reactive_group.findMany({
      where: { cargo_id: cargoAId },
      select: { reactive_group_id: true },
    }),
    prisma.cargo_reactive_group.findMany({
      where: { cargo_id: cargoBId },
      select: { reactive_group_id: true },
    }),
  ]);
  const groupsA = groupsARows.map((g) => g.reactive_group_id);
  const groupsB = groupsBRows.map((g) => g.reactive_group_id);
  if (groupsA.length === 0 || groupsB.length === 0) {
    return {
      compatible: null,
      source: "unknown",
      detail: { reason: "one or both cargoes have no reactive group" },
    };
  }

  // 4) Query the matrix for every group combination. The most restrictive result
  //    wins: if ANY group pair is incompatible, the cargo pair is incompatible.
  let matched = false;
  let incompatibleHit:
    | { group_a_id: number; group_b_id: number; reaction_description: string | null }
    | null = null;

  for (const x of groupsA) {
    for (const y of groupsB) {
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

  // 5) Return the matrix result.
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
