import type { PrismaClient } from "../generated/prisma/client.js";

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
 */

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

  const results = await prisma.cargo_chemical.findMany({
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
    include: {
      // -> cleaning_process
      cleaningProcesses: {
        // -> cleaning_process_step, in execution order
        include: { steps: { orderBy: { step_order: "asc" } } },
        orderBy: [{ cleaning_stage: "asc" }, { method_number: "asc" }],
      },
      // which source each cleaning process came from
      source: true,
    },
  });

  console.log("results", results.length, "cargo_chemical matches for", results);
  return results;
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
  return prisma.cargo_chemical.findMany({
    where: { canonical_name: { equals: name, mode: "insensitive" } },
    include: {
      cleaningProcesses: {
        include: { steps: { orderBy: { step_order: "asc" } } },
        orderBy: [{ cleaning_stage: "asc" }, { method_number: "asc" }],
      },
      cleaningProcessesFrom: {
        ...(toName
          ? { where: { to_cargo: { canonical_name: { equals: toName, mode: "insensitive" as const } } } }
          : {}),
        include: {
          to_cargo: { select: { id: true, canonical_name: true } },
          steps: { orderBy: { step_order: "asc" } },
        },
        orderBy: { to_cargo_id: "asc" },
      },
      source: true,
    },
  });
}

/**
 * The Verwey cleaning procedure for one specific pair: after `fromName`, to load
 * `toName`. Returns the cleaning_process (with procedure_code + ordered steps).
 */
export async function getPairProcedure(
  prisma: PrismaClient,
  fromName: string,
  toName: string,
) {
  return prisma.cleaning_process.findMany({
    where: {
      from_cargo: { canonical_name: { equals: fromName, mode: "insensitive" } },
      to_cargo: { canonical_name: { equals: toName, mode: "insensitive" } },
    },
    include: {
      from_cargo: { select: { canonical_name: true } },
      to_cargo: { select: { canonical_name: true } },
      steps: { orderBy: { step_order: "asc" } },
    },
  });
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
