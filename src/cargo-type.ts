import type { PrismaClient } from "../generated/prisma/client.js";
import { CargoType } from "../generated/prisma/enums.js";

/**
 * Service-layer half of the polymorphic cleaning_process / procedure_templates
 * design.
 *
 * cleaning_process.cargo_id / from_cargo_id / to_cargo_id hold an id from ONE
 * of three master tables and cargo_type says which:
 *
 *     CHEMICAL -> cargo_chemical
 *     OIL      -> crude_oil
 *     GAS      -> gas
 *
 * A PostgreSQL foreign key cannot retarget itself per row, so those columns
 * carry no FK and Prisma models no relation for them. Integrity is enforced
 * twice instead: here, on the write path, and by the
 * cleaning_process_cargo_refs trigger in the database, which also catches
 * writes that never pass through this code (psql, the Python ETL loaders).
 *
 * Never dereference a cargo id without branching on cargo_type first.
 */

export { CargoType };

/** The master model each CargoType points at, for messages and lookups. */
export const CARGO_MASTER_TABLE: Record<CargoType, string> = {
  [CargoType.CHEMICAL]: "cargo_chemical",
  [CargoType.OIL]: "crude_oil",
  [CargoType.GAS]: "gas",
};

/** The three polymorphic columns, so callers and errors agree on the names. */
export type CargoRefs = {
  cargo_type: CargoType;
  cargo_id?: number | null;
  from_cargo_id?: number | null;
  to_cargo_id?: number | null;
};

export class CargoRefError extends Error {
  constructor(
    readonly field: keyof Omit<CargoRefs, "cargo_type">,
    readonly cargoType: CargoType,
    readonly id: number,
  ) {
    super(
      `${field}=${id} does not exist in ${CARGO_MASTER_TABLE[cargoType]} ` +
        `(cargo_type=${cargoType})`,
    );
    this.name = "CargoRefError";
  }
}

/**
 * Does `id` exist in the master table `cargoType` selects? A null/undefined id
 * is valid — all three columns are optional.
 */
export async function cargoExists(
  prisma: PrismaClient,
  cargoType: CargoType,
  id: number | null | undefined,
): Promise<boolean> {
  if (id == null) return true;

  switch (cargoType) {
    case CargoType.CHEMICAL:
      return (await prisma.cargo_chemical.count({ where: { id } })) > 0;
    case CargoType.OIL:
      return (await prisma.crude_oil.count({ where: { id } })) > 0;
    case CargoType.GAS:
      return (await prisma.gas.count({ where: { id } })) > 0;
  }
}

/**
 * Validate a cleaning_process write before it reaches the database. Throws
 * CargoRefError on the first id that does not resolve in the master table
 * cargo_type selects.
 *
 * Call this in front of every create/update — it is what stands in for the
 * foreign keys the polymorphic design cannot have.
 */
export async function assertCargoRefs(
  prisma: PrismaClient,
  refs: CargoRefs,
): Promise<void> {
  const fields = ["cargo_id", "from_cargo_id", "to_cargo_id"] as const;

  for (const field of fields) {
    const id = refs[field];
    if (id == null) continue;
    if (!(await cargoExists(prisma, refs.cargo_type, id))) {
      throw new CargoRefError(field, refs.cargo_type, id);
    }
  }

  // A transition is a pair: both ends or neither. Mirrors the
  // cleaning_process_pair_complete check constraint.
  if ((refs.from_cargo_id == null) !== (refs.to_cargo_id == null)) {
    throw new Error(
      "cleaning_process: from_cargo_id and to_cargo_id must be set together",
    );
  }

  // Single-cargo procedure or transition, never both. Mirrors
  // cleaning_process_cargo_xor_pair.
  if (refs.cargo_id != null && (refs.from_cargo_id != null || refs.to_cargo_id != null)) {
    throw new Error(
      "cleaning_process: cargo_id and a from/to pair are mutually exclusive",
    );
  }
}

/** Name of a cargo, whichever master it lives in. Null if the id is unknown. */
export async function cargoName(
  prisma: PrismaClient,
  cargoType: CargoType,
  id: number | null | undefined,
): Promise<string | null> {
  if (id == null) return null;

  switch (cargoType) {
    case CargoType.CHEMICAL: {
      const row = await prisma.cargo_chemical.findUnique({
        where: { id },
        select: { canonical_name: true },
      });
      return row?.canonical_name ?? null;
    }
    case CargoType.OIL: {
      const row = await prisma.crude_oil.findUnique({
        where: { id },
        select: { oil_name: true },
      });
      return row?.oil_name ?? null;
    }
    case CargoType.GAS: {
      const row = await prisma.gas.findUnique({
        where: { id },
        select: { gas_name: true },
      });
      return row?.gas_name ?? null;
    }
  }
}

/**
 * Resolve a cargo name to ids in the master table `cargoType` selects. Matching
 * is case-insensitive; several rows can come back because a cargo is stored
 * once per source.
 */
export async function findCargoIdsByName(
  prisma: PrismaClient,
  cargoType: CargoType,
  name: string,
): Promise<number[]> {
  const eq = { equals: name, mode: "insensitive" as const };

  switch (cargoType) {
    case CargoType.CHEMICAL:
      return (
        await prisma.cargo_chemical.findMany({
          where: { canonical_name: eq },
          select: { id: true },
        })
      ).map((r) => r.id);
    case CargoType.OIL:
      return (
        await prisma.crude_oil.findMany({
          where: { oil_name: eq },
          select: { id: true },
        })
      ).map((r) => r.id);
    case CargoType.GAS:
      return (
        await prisma.gas.findMany({ where: { gas_name: eq }, select: { id: true } })
      ).map((r) => r.id);
  }
}

/**
 * Create a cleaning_process row with the polymorphic ids validated first. Use
 * this rather than prisma.cleaning_process.create() so a bad reference fails
 * with a typed error instead of a raw trigger exception.
 */
export async function createCleaningProcess(
  prisma: PrismaClient,
  data: CargoRefs & Record<string, unknown>,
) {
  await assertCargoRefs(prisma, data);
  return prisma.cleaning_process.create({ data: data as never });
}

/**
 * The cleaning rule(s) for a transition, in any cargo type.
 *
 * Example — after Diesel, to load Gasoline, per the Shell matrix:
 *   getTransition(prisma, CargoType.OIL, "Diesel", "Gasoline")
 *
 * Several rows can come back for one pair: the source may make the procedure
 * conditional ("CWM if Chemical Grade, otherwise NC"), and each condition is
 * its own row. Read `condition` before acting on `procedure_template`.
 */
export async function getTransition(
  prisma: PrismaClient,
  cargoType: CargoType,
  fromName: string,
  toName: string,
) {
  const [fromIds, toIds] = await Promise.all([
    findCargoIdsByName(prisma, cargoType, fromName),
    findCargoIdsByName(prisma, cargoType, toName),
  ]);
  if (fromIds.length === 0 || toIds.length === 0) return [];

  return prisma.cleaning_process.findMany({
    where: {
      cargo_type: cargoType,
      from_cargo_id: { in: fromIds },
      to_cargo_id: { in: toIds },
    },
    include: {
      procedure_template: {
        include: {
          procedureTemplateSteps: { orderBy: { step_order: "asc" } },
          procedureTemplateInstructions: { orderBy: { display_order: "asc" } },
        },
      },
      steps: { orderBy: { step_order: "asc" } },
      source: { select: { id: true, name: true, category: true } },
    },
    orderBy: { id: "asc" },
  });
}
