import "dotenv/config";
import { PrismaPg } from "@prisma/adapter-pg";
import { PrismaClient } from "./generated/prisma/client.js";
import { getCleaningProcess } from "./src/cleaning.js";

const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: process.env["DATABASE_URL"] }) });
for (const q of ["acetic acid", "ethanoic acid", "vinegar acid"]) {
  const res = await getCleaningProcess(prisma, q);
  const withClean = res.filter((c) => c.cleaningProcesses.length > 0);
  console.log(`\nquery=${JSON.stringify(q)} -> matched ${res.length} cargo, ${withClean.length} with cleaning`);
  for (const c of withClean)
    for (const p of c.cleaningProcesses) {
      console.log(`  [${c.canonical_name} @ ${c.source?.name}] ${p.cleaning_stage} method#${p.method_number}`);
      for (const s of p.steps) console.log(`     ${s.step_order}. ${s.method ?? ""} | ${s.medium ?? ""} | ${s.temperature ?? ""} | ${s.duration ?? ""}`);
    }
}
await prisma.$disconnect();
