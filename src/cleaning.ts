import type { PrismaClient } from "../generated/prisma/client.js";
import { CargoType } from "../generated/prisma/enums.js";

/**
 * Look up the tank-cleaning process(es) for a chemical by name or synonym.
 *
 * A chemical is stored once per source (e.g. LARS, CHEM, Miracle), but only some
 * sources carry cleaning steps (currently the Miracle Tank Cleaning Guide). The
 * match is case-insensitive, resolves synonyms (e.g. "Ethanoic acid" ->
 * "Acetic acid"), and sweeps every source copy so cleaning data is never missed.
 *
 * Data path: cargo_chemical -> cleaning_process (stage + method) ->
 * cleaning_process_step (ordered steps).
 *
 * cleaning_process is COMMON to chemicals, crude oils and gases, so its cargo
 * columns carry no foreign key and Prisma models no relation for them (see
 * src/cargo-type.ts). Every query below therefore resolves cargo ids first and
 * filters on `cargo_type: CHEMICAL` explicitly — an id alone is ambiguous
 * across the three masters.
 */

/** What every cleaning lookup here pulls back with a cleaning_process row. */
const CLEANING_INCLUDE = {
  steps: { orderBy: { step_order: "asc" } },
  procedure_template: { select: { id: true, procedure_code: true, template_name: true } },
  source: { select: { id: true, name: true, category: true } },
} as const;

/**
 * Normalise a name the same way the importer does (see master_loader
 * normalize_synonym): lowercase, strip punctuation, collapse whitespace. Lets a
 * query match synonyms.normalized_text regardless of punctuation/casing.
 */
export function normalizeName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export async function getCleaningProcess(prisma: PrismaClient, name: string) {
  const normalized = normalizeName(name);

  const cargoes = await prisma.cargo_chemical.findMany({
    where: {
      OR: [
        // direct canonical-name match
        { canonical_name: { equals: name, mode: "insensitive" } },
        // synonym match (raw text or normalized form)
        {
          cargoSynonyms: {
            some: {
              synonyms: {
                OR: [
                  { synonym_text: { equals: name, mode: "insensitive" } },
                  { normalized_text: normalized },
                ],
              },
            },
          },
        },
      ],
    },
    include: { source: true },
  });

  // One query for every matched cargo copy, then regrouped, rather than an
  // include: the link is polymorphic, so Prisma cannot join it for us.
  const processes = await prisma.cleaning_process.findMany({
    where: {
      cargo_type: CargoType.CHEMICAL,
      cargo_id: { in: cargoes.map((c) => c.id) },
    },
    include: CLEANING_INCLUDE,
    orderBy: [{ cleaning_stage: "asc" }, { method_number: "asc" }],
  });

  return cargoes.map((cargo) => ({
    ...cargo,
    cleaningProcesses: processes.filter((p) => p.cargo_id === cargo.id),
  }));
}

/**
 * Everything cleaning-related for a cargo, looked up by canonical_name
 * (case-insensitive). Returns each matching cargo_chemical row with:
 *
 *   - cleaningProcesses     : the cargo's OWN procedures (ChemServe/SDS/Miracle),
 *                             each with its ordered steps.
 *   - cleaningProcessesFrom : the Dr. Verwey FROM->TO matrix rows where THIS cargo
 *                             is the *previous* cargo — i.e. "after carrying this
 *                             cargo, to load <next cargo> run <procedure_code>",
 *                             including the next cargo's name and the copied steps.
 *
 * All step lists come from cleaning_process_step (method / medium / temperature /
 * duration / cleaner / description / mandatory), ordered by step_order.
 *
 * Pass `toName` to narrow the matrix to a single previous->next pair.
 */
export async function getCargoCleaning(
  prisma: PrismaClient,
  name: string,
  toName?: string,
) {
  const cargoes = await prisma.cargo_chemical.findMany({
    where: { canonical_name: { equals: name, mode: "insensitive" } },
    include: { source: true },
  });
  if (cargoes.length === 0) return [];

  const cargoIds = cargoes.map((c) => c.id);

  // Optional narrowing to one next-cargo. Resolved to ids up front because
  // to_cargo_id has no relation to filter through.
  const toIds = toName
    ? (
        await prisma.cargo_chemical.findMany({
          where: { canonical_name: { equals: toName, mode: "insensitive" } },
          select: { id: true },
        })
      ).map((r) => r.id)
    : undefined;

  const [own, from] = await Promise.all([
    prisma.cleaning_process.findMany({
      where: { cargo_type: CargoType.CHEMICAL, cargo_id: { in: cargoIds } },
      include: CLEANING_INCLUDE,
      orderBy: [{ cleaning_stage: "asc" }, { method_number: "asc" }],
    }),
    prisma.cleaning_process.findMany({
      where: {
        cargo_type: CargoType.CHEMICAL,
        from_cargo_id: { in: cargoIds },
        ...(toIds ? { to_cargo_id: { in: toIds } } : {}),
      },
      include: CLEANING_INCLUDE,
      orderBy: { to_cargo_id: "asc" },
    }),
  ]);

  // The next cargo's name, resolved in one sweep — to_cargo_id is polymorphic,
  // so there is no relation to include it through.
  const toNames = await namesById(
    prisma,
    from.map((p) => p.to_cargo_id),
  );

  return cargoes.map((cargo) => ({
    ...cargo,
    cleaningProcesses: own.filter((p) => p.cargo_id === cargo.id),
    cleaningProcessesFrom: from
      .filter((p) => p.from_cargo_id === cargo.id)
      .map((p) => ({
        ...p,
        to_cargo:
          p.to_cargo_id == null
            ? null
            : { id: p.to_cargo_id, canonical_name: toNames.get(p.to_cargo_id) ?? null },
      })),
  }));
}

/**
 * The cleaning procedure for one specific pair: after `fromName`, to load
 * `toName`. Returns the cleaning_process rows (with procedure_code, the
 * condition the source attaches to them, and ordered steps).
 *
 * More than one row per pair is normal where the source makes the procedure
 * conditional ("CWM if Chemical Grade, otherwise NC") — check `condition`.
 */
export async function getPairProcedure(
  prisma: PrismaClient,
  fromName: string,
  toName: string,
) {
  const idsFor = (n: string) =>
    prisma.cargo_chemical
      .findMany({
        where: { canonical_name: { equals: n, mode: "insensitive" } },
        select: { id: true },
      })
      .then((rows) => rows.map((r) => r.id));

  const [fromIds, toIds] = await Promise.all([idsFor(fromName), idsFor(toName)]);
  if (fromIds.length === 0 || toIds.length === 0) return [];

  const processes = await prisma.cleaning_process.findMany({
    where: {
      cargo_type: CargoType.CHEMICAL,
      from_cargo_id: { in: fromIds },
      to_cargo_id: { in: toIds },
    },
    include: CLEANING_INCLUDE,
  });

  const names = await namesById(prisma, [
    ...processes.map((p) => p.from_cargo_id),
    ...processes.map((p) => p.to_cargo_id),
  ]);

  return processes.map((p) => ({
    ...p,
    from_cargo:
      p.from_cargo_id == null ? null : { canonical_name: names.get(p.from_cargo_id) ?? null },
    to_cargo:
      p.to_cargo_id == null ? null : { canonical_name: names.get(p.to_cargo_id) ?? null },
  }));
}

/** canonical_name for a batch of cargo_chemical ids, nulls and repeats ignored. */
async function namesById(
  prisma: PrismaClient,
  ids: (number | null)[],
): Promise<Map<number, string>> {
  const unique = [...new Set(ids.filter((id): id is number => id != null))];
  if (unique.length === 0) return new Map();

  const rows = await prisma.cargo_chemical.findMany({
    where: { id: { in: unique } },
    select: { id: true, canonical_name: true },
  });
  return new Map(rows.map((r) => [r.id, r.canonical_name]));
}

// Example usage:
//
//   import { PrismaClient } from "../generated/prisma/client.js";
//   const prisma = new PrismaClient();
//
//   // all cleaning for a cargo (own procedures + "from this cargo" matrix):
//   const [cargo] = await getCargoCleaning(prisma, "acetone");
//   for (const p of cargo.cleaningProcesses)
//     console.log("own:", p.cleaning_stage, p.steps.length, "steps");
//   for (const m of cargo.cleaningProcessesFrom)
//     console.log(`-> ${m.to_cargo?.canonical_name}: procedure ${m.procedure_code} (${m.steps.length} steps)`);
//
//   // one specific pair (previous -> next):
//   const [pair] = await getPairProcedure(prisma, "acetone", "acetic acid");
//   console.log(pair.procedure_code, pair.steps.map(s => s.method));
//
//   // the same question for a crude oil or a gas — see src/cargo-type.ts:
//   //   getTransition(prisma, CargoType.OIL, "Diesel", "Gasoline");
