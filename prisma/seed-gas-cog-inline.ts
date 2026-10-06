/**
 * Seed: Norgas Class COG Time Estimation -> cargo_gas, cleaning_process,
 * cleaning_process_step. Existing tables only; no schema change, no migration.
 *
 * SHAPE
 * -----
 *   8 gases                          -> cargo_gas            (per-source rows)
 *   56 transitions x 8 ship classes  -> cleaning_process     448 rows
 *   one row per operation            -> cleaning_process_step ~2208 rows
 *
 * A row is { cargo_type: GAS, cargo_id: null, from_cargo_id, to_cargo_id },
 * the shape both assertCargoRefs() and the cleaning_process_cargo_refs trigger
 * accept.
 *
 * WHY THE SHIP CLASS IS IN `condition`
 * ------------------------------------
 * cleaning_process_pair_key is UNIQUE (from_cargo_id, to_cargo_id, source_id,
 * COALESCE(condition,'')). `title` is not part of it, so eight ship-class rows
 * for one transition that differ only by title collide on the second insert
 * (verified: 23505 on cleaning_process_pair_key). The estimate genuinely applies
 * only to one class of ship, which is what `condition` means here - getTransition()
 * already expects one row per condition. Putting the class there makes the 448
 * rows distinct AND gives re-runs an idempotency key through the existing index,
 * so no migration or new index is needed. The class is also in `title` and
 * `notes` for readability.
 *
 * DURATIONS
 * ---------
 * `duration` holds the hours inline ("79.24 hrs"), rounded to 2 dp for reading.
 * The figure exactly as the source printed it ("79.24285714285715") is kept in
 * the step's `remarks`, so the rounding is auditable and never the only copy.
 *
 * STEP TYPES
 * ----------
 * WARMING_DEPRESSURIZING, GASSING_UP and COOLING_DOWN have no CleaningStepType
 * value and are left NULL on purpose (the enum is not extended here). The other
 * four map to existing values. An operation not listed in OP_META stops the load.
 *
 * `mandatory` is left NULL: these are time ESTIMATES for planning, not steps a
 * guide mandates.
 *
 * IDEMPOTENCY
 * -----------
 * Everything is validated before anything is written, then written in ONE
 * interactive transaction. cargo_gas upserts on (gas_name, source_id);
 * cleaning_process is matched on its pair_key columns and updated or created;
 * steps upsert on (cleaning_process_id, step_order), and any step_order a re-run
 * no longer produces is deleted so a shortened estimate does not keep stale steps.
 *
 * Usage:  npx tsx prisma/seed-gas-cog-inline.ts
 */
import "dotenv/config";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "../generated/prisma/client.js";
import { CargoType, CleaningStepType, SourceCategory } from "../generated/prisma/enums.js";
import { assertCargoRefs } from "../src/cargo-type.js";

type Operation = { operation: string; duration_hrs: number; raw_value: string; step_order: number };
type Estimate = {
  ship_class: string;
  purging_included: boolean;
  total_hrs: number;
  total_days: number;
  operations: Operation[];
};
type Transition = {
  from_gas: string;
  to_gas: string;
  requirements: string;
  method_notes: string;
  estimates: Estimate[];
};
type ShipClass = {
  class_code: string;
  class_name: string;
  tank_capacity_m3: number;
  dry_air_capacity: number;
  notes: string;
};
type CogData = {
  source: { name: string; edition: string; publisher: string; category: string; source_type: string; notes: string };
  ship_classes: ShipClass[];
  gases: { gas_name: string }[];
  transitions: Transition[];
};

type StepTypeValue = (typeof CleaningStepType)[keyof typeof CleaningStepType];

// operation -> readable method name and the CleaningStepType it maps to (null = no enum value yet).
const OP_META: Record<string, { label: string; step_type: StepTypeValue | null }> = {
  WARMING_DEPRESSURIZING: { label: "Warming up / depressurizing", step_type: null },
  PURGING_INERTING: { label: "Purging / inerting", step_type: CleaningStepType.PURGING },
  GASSING_UP: { label: "Gassing up", step_type: null },
  COOLING_DOWN: { label: "Cooling down", step_type: null },
  GAS_FREEING: { label: "Gas freeing", step_type: CleaningStepType.GAS_FREEING },
  AERATION: { label: "Aeration", step_type: CleaningStepType.VENTILATING },
  VISUAL_INSPECTION: { label: "Visual inspection", step_type: CleaningStepType.INSPECTION },
};

const DATA_FILE = path.join(__dirname, "norgas_cog_data.json");
const RANK_CLEANING = 3;

function hours(h: number): string {
  return `${Number(h.toFixed(2))} hrs`;
}

function validate(d: CogData): string[] {
  const errors: string[] = [];
  const gases = new Set(d.gases.map((g) => g.gas_name));
  const classes = new Set(d.ship_classes.map((s) => s.class_code));
  if (gases.size !== d.gases.length) errors.push("gases list contains a duplicate name");
  const seenPair = new Set<string>();
  for (const t of d.transitions) {
    const pair = `${t.from_gas} -> ${t.to_gas}`;
    if (!gases.has(t.from_gas)) errors.push(`${pair}: from_gas not in gases list`);
    if (!gases.has(t.to_gas)) errors.push(`${pair}: to_gas not in gases list`);
    if (t.from_gas === t.to_gas) errors.push(`${pair}: a gas cannot follow itself`);
    const seenClass = new Set<string>();
    for (const e of t.estimates) {
      const key = `${pair} (${e.ship_class})`;
      if (!classes.has(e.ship_class)) errors.push(`${key}: unknown ship class`);
      if (seenClass.has(e.ship_class)) errors.push(`${key}: ship class repeated`);
      seenClass.add(e.ship_class);
      if (seenPair.has(key)) errors.push(`${key}: duplicated`);
      seenPair.add(key);
      const orders = new Set<number>();
      for (const o of e.operations) {
        if (!(o.operation in OP_META)) errors.push(`${key}: unknown operation ${o.operation}`);
        if (typeof o.duration_hrs !== "number" || Number.isNaN(o.duration_hrs))
          errors.push(`${key}: ${o.operation} has no numeric duration`);
        if (orders.has(o.step_order)) errors.push(`${key}: step_order ${o.step_order} repeated`);
        orders.add(o.step_order);
      }
    }
  }
  return errors;
}

async function main(): Promise<void> {
  const data = JSON.parse(readFileSync(DATA_FILE, "utf8")) as CogData;
  const errors = validate(data);
  if (errors.length) {
    console.error(`${errors.length} validation error(s) - nothing written:`);
    for (const e of errors) console.error(`  ${e}`);
    process.exitCode = 1;
    return;
  }
  const classByCode = new Map(data.ship_classes.map((s) => [s.class_code, s]));

  const prisma = new PrismaClient({
    adapter: new PrismaPg({ connectionString: process.env["DATABASE_URL"] }),
  });

  try {
    const counts = await prisma.$transaction(
      async (tx) => {
        // --- source ----------------------------------------------------------
        const src = data.source;
        const sourceFields = {
          edition: src.edition,
          publisher: src.publisher,
          category: SourceCategory.gas,
          source_type: src.source_type,
          notes: src.notes,
          rank_cleaning: RANK_CLEANING,
        };
        const existing = await tx.source.findFirst({ where: { name: src.name }, select: { id: true } });
        const source = existing
          ? await tx.source.update({ where: { id: existing.id }, data: sourceFields })
          : await tx.source.create({ data: { name: src.name, ...sourceFields } });

        // --- gases -----------------------------------------------------------
        const gasId = new Map<string, number>();
        for (const g of data.gases) {
          const row = await tx.cargo_gas.upsert({
            where: { gas_name_source_id: { gas_name: g.gas_name, source_id: source.id } },
            update: {},
            create: { gas_name: g.gas_name, source_id: source.id },
          });
          gasId.set(g.gas_name, row.id);
        }

        // --- transitions x ship class, and their steps -----------------------
        let processes = 0;
        let steps = 0;
        for (const t of data.transitions) {
          const fromId = gasId.get(t.from_gas);
          const toId = gasId.get(t.to_gas);
          if (fromId === undefined || toId === undefined) throw new Error(`unresolved gas in ${t.from_gas} -> ${t.to_gas}`);

          for (const e of t.estimates) {
            const cls = classByCode.get(e.ship_class);
            if (!cls) throw new Error(`unknown ship class ${e.ship_class}`);

            const refs = { cargo_type: CargoType.GAS, cargo_id: null, from_cargo_id: fromId, to_cargo_id: toId };
            // assertCargoRefs is typed for the full client; the transaction client has the
            // same model delegates it uses (.count), and must be used so it can see the
            // cargo_gas rows created above in this still-open transaction.
            await assertCargoRefs(tx as unknown as PrismaClient, refs);

            const condition = `Ship class ${cls.class_code} (${cls.class_name})`;
            const fields = {
              ...refs,
              source_id: source.id,
              condition,
              title: `${t.from_gas} -> ${t.to_gas} (${cls.class_code})`,
              remarks: t.requirements,
              source_page_ref: path.basename(DATA_FILE),
              notes:
                `Ship class ${cls.class_code} (${cls.class_name}; ${cls.tank_capacity_m3} m3 tanks, ` +
                `dry air capacity ${cls.dry_air_capacity}; ${cls.notes}). ` +
                `Estimated total ${e.total_hrs} h (${e.total_days} days); purging ` +
                `${e.purging_included ? "included" : "NOT included"} in the estimate. ` +
                `Method: ${t.method_notes} ` +
                `These are time ESTIMATES for planning a change of grade, not mandated steps. ` +
                `Source: ${src.name} (${src.edition}).`,
            };

            const found = await tx.cleaning_process.findFirst({
              where: { cargo_type: CargoType.GAS, from_cargo_id: fromId, to_cargo_id: toId, source_id: source.id, condition },
              select: { id: true },
            });
            const process = found
              ? await tx.cleaning_process.update({ where: { id: found.id }, data: fields })
              : await tx.cleaning_process.create({ data: fields });
            processes++;

            const orders: number[] = [];
            for (const o of e.operations) {
              const meta = OP_META[o.operation];
              if (!meta) throw new Error(`unknown operation ${o.operation}`);
              const stepData = {
                method: meta.label,
                step_type: meta.step_type,
                duration: hours(o.duration_hrs),
                description: `${meta.label} (${o.operation}).`,
                remarks: `Duration as printed by the source: ${o.raw_value} h. Shown rounded to 2 dp in duration.`,
                mandatory: null,
              };
              await tx.cleaning_process_step.upsert({
                where: { cleaning_process_id_step_order: { cleaning_process_id: process.id, step_order: o.step_order } },
                update: stepData,
                create: { cleaning_process_id: process.id, step_order: o.step_order, ...stepData },
              });
              orders.push(o.step_order);
              steps++;
            }
            // A re-run with fewer operations must not leave the old tail behind.
            await tx.cleaning_process_step.deleteMany({
              where: { cleaning_process_id: process.id, step_order: { notIn: orders } },
            });
          }
        }
        return { gases: gasId.size, processes, steps };
      },
      { maxWait: 30_000, timeout: 600_000 },
    );

    console.log(`gases: ${counts.gases}`);
    console.log(`cleaning_process (transition x ship): ${counts.processes}`);
    console.log(`cleaning_process_step (upserts): ${counts.steps}`);
    console.log("done.");
  } finally {
    await prisma.$disconnect();
  }
}

main().catch((err: unknown) => {
  console.error(err);
  process.exitCode = 1;
});
