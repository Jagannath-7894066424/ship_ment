import "dotenv/config";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "../generated/prisma/client.js";
import { getCleaningProcess } from "./cleaning.js";

// Usage: npx tsx src/run-cleaning.ts "acetic acid"
async function main() {
  const name = process.argv.slice(2).join(" ") || "acetic acid";

  const adapter = new PrismaPg({ connectionString: process.env["DATABASE_URL"] });
  const prisma = new PrismaClient({ adapter });

  try {
    const results = await getCleaningProcess(prisma, name);
    const withCleaning = results.filter((c) => c.cleaningProcesses.length > 0);

    console.log(`\nQuery: ${JSON.stringify(name)}`);
    console.log(`Matched ${results.length} chemical row(s); ${withCleaning.length} with cleaning data.\n`);

    for (const cargo of withCleaning) {
      for (const proc of cargo.cleaningProcesses) {
        console.log(`${cargo.canonical_name}  [${cargo.source?.name}]  ${proc.cleaning_stage} — method #${proc.method_number}`);
        if (proc.title) console.log(`  ${proc.title}`);
        for (const s of proc.steps) {
          console.log(`   ${s.step_order}. ${s.method ?? ""}` +
            [s.medium, s.temperature, s.duration, s.cleaner].filter(Boolean).map((x) => ` | ${x}`).join(""));
        }
        console.log();
      }
    }
  } finally {
    await prisma.$disconnect();
  }
}

main();
