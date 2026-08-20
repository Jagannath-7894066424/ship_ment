-- Procedure-level instructions for a cleaning procedure template.
--
-- procedure_template_steps holds the numbered steps. The guides also carry
-- statements that apply to the procedure as a whole — the "NOTE :" blocks that
-- follow (or, more rarely, precede) the steps — which have no position in the
-- step sequence and are classified by severity instead. Those live here.
--
-- Currently that text is squashed into procedure_templates.notes (one joined
-- string per template); this table is where the split-out, classified rows go.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'InstructionType') THEN
    CREATE TYPE "InstructionType" AS ENUM ('DANGER', 'WARNING', 'CAUTION', 'IMPORTANT', 'INFO');
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS "procedure_template_instruction" (
    "id"                     SERIAL            PRIMARY KEY,
    "procedure_templates_id" INTEGER           NOT NULL,
    "instruction_type"       "InstructionType" NOT NULL,
    "message"                TEXT              NOT NULL,
    "display_order"          INTEGER           NOT NULL,
    "mandatory"              BOOLEAN           NOT NULL DEFAULT true,
    "notes"                  TEXT,
    "created_at"             TIMESTAMP(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"             TIMESTAMP(3)      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "procedure_template_instruction_procedure_templates_id_fkey"
        FOREIGN KEY ("procedure_templates_id") REFERENCES "procedure_templates"("id")
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE INDEX IF NOT EXISTS "procedure_template_instruction_procedure_templates_id_idx"
    ON "procedure_template_instruction"("procedure_templates_id");
CREATE INDEX IF NOT EXISTS "procedure_template_instruction_instruction_type_idx"
    ON "procedure_template_instruction"("instruction_type");

-- One instruction per display slot, so a re-extract can upsert in place instead
-- of accumulating duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS "procedure_template_instruction_procedure_templates_id_display_order_key"
    ON "procedure_template_instruction"("procedure_templates_id", "display_order");
