"""Write eval/cases.json from the catalog. Run once; the file is then frozen."""
from mend.cases import write_cases, load_cases

write_cases()
cases = load_cases()
print(f"wrote {len(cases)} cases:", {s: sum(c.split == s for c in cases) for s in ("dev", "test")})
