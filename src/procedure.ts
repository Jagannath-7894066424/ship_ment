import type { PrismaClient } from "../generated/prisma/client.js";

/**
 * Reading a cleaning procedure back out whole.
 *
 * A procedure is stored in four tables because its parts answer four different
 * questions, and flattening them would lose information:
 *
 *   procedure_templates              what the code means            "CW = Cold Water Wash"
 *   procedure_template_steps         ordered actions                1..n, sequenced
 *   procedure_template_requirement   rules, limits, disjunctions    unsequenced
 *   procedure_template_instruction   the source's own statements    unsequenced
 *
 * These functions put them back together for a caller. Ordering is fixed:
 * steps by step_order, requirements and instructions by display_order.
 */

/** Everything a caller needs to display or act on one procedure. */
const PROCEDURE_INCLUDE = {
  procedureTemplateSteps: { orderBy: { step_order: "asc" } },
  procedureTemplateRequirements: { orderBy: { display_order: "asc" } },
  procedureTemplateInstructions: { orderBy: { display_order: "asc" } },
  source: { select: { id: true, name: true, category: true } },
} as const;

export type CompleteProcedure = Awaited<ReturnType<typeof getCompleteProcedure>>;

/**
 * One procedure, whole.
 *
 * Keyed on (source_id, procedure_code) because a code means nothing without its
 * document: Verwey's "A" and Shell's "CW" live in the same table and must not be
 * confused. Returns null when the source defines no such code.
 *
 *   const cfw = await getCompleteProcedure(prisma, 24, "CFW");
 *
 * `loading_allowed: false` (NC) means the transition is forbidden; such a
 * procedure legitimately has an empty `steps` array — that is not missing data.
 */
export async function getCompleteProcedure(
  prisma: PrismaClient,
  sourceId: number,
  procedureCode: string,
) {
  const template = await prisma.procedure_templates.findUnique({
    where: { source_id_procedure_code: { source_id: sourceId, procedure_code: procedureCode } },
    include: PROCEDURE_INCLUDE,
  });
  return template ? shape(template) : null;
}

/** Every procedure a source defines, in code order. */
export async function getProceduresForSource(prisma: PrismaClient, sourceId: number) {
  const templates = await prisma.procedure_templates.findMany({
    where: { source_id: sourceId },
    include: PROCEDURE_INCLUDE,
    orderBy: { procedure_code: "asc" },
  });
  return templates.map(shape);
}

/**
 * The procedure a cargo transition calls for, resolved through
 * cleaning_process.procedure_template_id.
 *
 * This is the whole point of keeping the two tables apart: cleaning_process says
 * *which* procedure applies between two cargoes and never restates it, so a
 * correction to the procedure reaches every transition that cites it.
 *
 * Returns the transition's own context (condition, remarks, source page) beside
 * the procedure — a matrix cell is often conditional, and acting on the
 * procedure without reading `condition` would apply a rule out of its context.
 */
export async function getProcedureForTransition(
  prisma: PrismaClient,
  cleaningProcessId: number,
) {
  const process = await prisma.cleaning_process.findUnique({
    where: { id: cleaningProcessId },
    include: {
      procedure_template: { include: PROCEDURE_INCLUDE },
      source: { select: { id: true, name: true, category: true } },
    },
  });
  if (!process) return null;

  return {
    cleaning_process_id: process.id,
    cargo_type: process.cargo_type,
    from_cargo_id: process.from_cargo_id,
    to_cargo_id: process.to_cargo_id,
    condition: process.condition,
    remarks: process.remarks,
    source_page_ref: process.source_page_ref,
    procedure: process.procedure_template ? shape(process.procedure_template) : null,
  };
}

type TemplateWithChildren = Awaited<
  ReturnType<PrismaClient["procedure_templates"]["findUniqueOrThrow"]>
> & {
  procedureTemplateSteps: any[];
  procedureTemplateRequirements: any[];
  procedureTemplateInstructions: any[];
  source: { id: number; name: string; category: string };
};

/** Flatten the Prisma rows into the response shape, dropping bookkeeping columns. */
function shape(t: TemplateWithChildren) {
  return {
    procedure_code: t.procedure_code,
    template_name: t.template_name,
    cargo_type: t.cargo_type,
    water_type: t.water_type,
    loading_allowed: t.loading_allowed,
    description: t.description,
    // The source's own wording, kept next to the structured rows rather than
    // replaced by them — normalisation is lossy and this is the audit trail.
    source_definition: t.source_definition,
    source_page_ref: t.source_page_ref,
    source: t.source,

    steps: t.procedureTemplateSteps.map((s) => ({
      step_order: s.step_order,
      step_name: s.step_name,
      step_type: s.step_type,
      medium: s.medium,
      temperature: s.temperature,
      duration: s.duration,
      cleaner: s.cleaner,
      mandatory: s.mandatory,
      step_description: s.step_description,
    })),

    // Where a source says "ventilate OR purge", that arrives here as ONE
    // requirement (ATMOSPHERE_TREATMENT = VENTILATE_OR_PURGE), never as two
    // mandatory obligations.
    requirements: t.procedureTemplateRequirements.map((r) => ({
      requirement_type: r.requirement_type,
      requirement_value: r.requirement_value,
      operator: r.operator,
      unit: r.unit,
      mandatory: r.mandatory,
      description: r.description,
      display_order: r.display_order,
    })),

    instructions: t.procedureTemplateInstructions.map((i) => ({
      instruction_type: i.instruction_type,
      message: i.message,
      mandatory: i.mandatory,
      display_order: i.display_order,
    })),
  };
}
