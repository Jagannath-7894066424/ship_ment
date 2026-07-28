-- CreateEnum
CREATE TYPE "CleaningStage" AS ENUM ('BEFORE_LOADING', 'AFTER_DISCHARGE');

-- DropForeignKey
ALTER TABLE "cleaning_procedures" DROP CONSTRAINT "cleaning_procedures_from_cargo_id_fkey";

-- DropForeignKey
ALTER TABLE "cleaning_procedures" DROP CONSTRAINT "cleaning_procedures_source_id_fkey";

-- DropForeignKey
ALTER TABLE "cleaning_procedures" DROP CONSTRAINT "cleaning_procedures_target_cleanliness_standard_id_fkey";

-- DropForeignKey
ALTER TABLE "cleaning_procedures" DROP CONSTRAINT "cleaning_procedures_template_id_fkey";

-- DropForeignKey
ALTER TABLE "cleaning_procedures" DROP CONSTRAINT "cleaning_procedures_to_cargo_id_fkey";

-- AlterTable
ALTER TABLE "cargo_chemical" ADD COLUMN     "appearance" TEXT,
ADD COLUMN     "lel" DOUBLE PRECISION,
ADD COLUMN     "molecular_formula" TEXT,
ADD COLUMN     "odour" TEXT,
ADD COLUMN     "product_description" TEXT,
ADD COLUMN     "uel" DOUBLE PRECISION;

-- AlterTable
ALTER TABLE "cargo_property_values" ADD COLUMN     "normalized_value" DOUBLE PRECISION,
ADD COLUMN     "unit" TEXT,
ADD COLUMN     "value_type" TEXT;

-- DropTable
DROP TABLE "cleaning_procedures";

-- DropEnum
DROP TYPE "fire_protection";

-- DropEnum
DROP TYPE "flashpoint_requirement";

-- DropEnum
DROP TYPE "vapour_detection";

-- CreateTable
CREATE TABLE "cleaning_process" (
    "id" SERIAL NOT NULL,
    "cargo_id" INTEGER NOT NULL,
    "source_id" INTEGER NOT NULL,
    "cleaning_stage" "CleaningStage" NOT NULL,
    "method_number" INTEGER NOT NULL,
    "title" TEXT,
    "recipe_for" TEXT,
    "source_page_ref" TEXT,
    "notes" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "cleaning_process_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "cleaning_process_step" (
    "id" SERIAL NOT NULL,
    "cleaning_process_id" INTEGER NOT NULL,
    "step_order" INTEGER NOT NULL,
    "method" TEXT,
    "duration" TEXT,
    "temperature" TEXT,
    "medium" TEXT,
    "cleaner" TEXT,
    "description" TEXT,
    "remarks" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "cleaning_process_step_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "coating_company" (
    "id" SERIAL NOT NULL,
    "name" TEXT NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "coating_company_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "coating_system" (
    "id" SERIAL NOT NULL,
    "company_id" INTEGER NOT NULL,
    "system_name" TEXT NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "coating_system_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "cargo_coating" (
    "id" SERIAL NOT NULL,
    "cargo_id" INTEGER NOT NULL,
    "source_id" INTEGER NOT NULL,
    "coating_system_id" INTEGER NOT NULL,
    "rating" TEXT,
    "notes" TEXT,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "cargo_coating_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "cleaning_process_cargo_id_idx" ON "cleaning_process"("cargo_id");

-- CreateIndex
CREATE INDEX "cleaning_process_source_id_idx" ON "cleaning_process"("source_id");

-- CreateIndex
CREATE INDEX "cleaning_process_cargo_id_source_id_idx" ON "cleaning_process"("cargo_id", "source_id");

-- CreateIndex
CREATE UNIQUE INDEX "cleaning_process_cargo_id_source_id_cleaning_stage_method_n_key" ON "cleaning_process"("cargo_id", "source_id", "cleaning_stage", "method_number");

-- CreateIndex
CREATE INDEX "cleaning_process_step_cleaning_process_id_idx" ON "cleaning_process_step"("cleaning_process_id");

-- CreateIndex
CREATE UNIQUE INDEX "cleaning_process_step_cleaning_process_id_step_order_key" ON "cleaning_process_step"("cleaning_process_id", "step_order");

-- CreateIndex
CREATE UNIQUE INDEX "coating_company_name_key" ON "coating_company"("name");

-- CreateIndex
CREATE INDEX "coating_system_company_id_idx" ON "coating_system"("company_id");

-- CreateIndex
CREATE UNIQUE INDEX "coating_system_company_id_system_name_key" ON "coating_system"("company_id", "system_name");

-- CreateIndex
CREATE INDEX "cargo_coating_cargo_id_idx" ON "cargo_coating"("cargo_id");

-- CreateIndex
CREATE INDEX "cargo_coating_source_id_idx" ON "cargo_coating"("source_id");

-- CreateIndex
CREATE INDEX "cargo_coating_coating_system_id_idx" ON "cargo_coating"("coating_system_id");

-- CreateIndex
CREATE INDEX "cargo_coating_cargo_id_source_id_idx" ON "cargo_coating"("cargo_id", "source_id");

-- CreateIndex
CREATE INDEX "cargo_chemical_canonical_name_idx" ON "cargo_chemical"("canonical_name");

-- CreateIndex
CREATE INDEX "cargo_chemical_cas_number_idx" ON "cargo_chemical"("cas_number");

-- CreateIndex
CREATE INDEX "cargo_chemical_un_number_idx" ON "cargo_chemical"("un_number");

-- CreateIndex
CREATE INDEX "cargo_chemical_source_id_idx" ON "cargo_chemical"("source_id");

-- CreateIndex
CREATE INDEX "cargo_property_values_cargo_id_idx" ON "cargo_property_values"("cargo_id");

-- CreateIndex
CREATE INDEX "cargo_property_values_source_id_idx" ON "cargo_property_values"("source_id");

-- CreateIndex
CREATE INDEX "cargo_property_values_field_name_idx" ON "cargo_property_values"("field_name");

-- CreateIndex
CREATE INDEX "cargo_property_values_cargo_id_source_id_idx" ON "cargo_property_values"("cargo_id", "source_id");

-- CreateIndex
CREATE INDEX "cargo_property_values_cargo_id_field_name_idx" ON "cargo_property_values"("cargo_id", "field_name");

-- AddForeignKey
ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_cargo_id_fkey" FOREIGN KEY ("cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "cleaning_process_step" ADD CONSTRAINT "cleaning_process_step_cleaning_process_id_fkey" FOREIGN KEY ("cleaning_process_id") REFERENCES "cleaning_process"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "coating_system" ADD CONSTRAINT "coating_system_company_id_fkey" FOREIGN KEY ("company_id") REFERENCES "coating_company"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "cargo_coating" ADD CONSTRAINT "cargo_coating_cargo_id_fkey" FOREIGN KEY ("cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "cargo_coating" ADD CONSTRAINT "cargo_coating_source_id_fkey" FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE CASCADE ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "cargo_coating" ADD CONSTRAINT "cargo_coating_coating_system_id_fkey" FOREIGN KEY ("coating_system_id") REFERENCES "coating_system"("id") ON DELETE CASCADE ON UPDATE CASCADE;

