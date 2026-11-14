"""Label eligibility sentences with letter codes using a strong model (teacher) on Groq, once.

Codes (several per sentence allowed):
  C citizenship / green card / U.S. person (ITAR/EAR) / security clearance / another country's citizenship required
  R F-1 / CPT / OPT / student visa holders rejected, or PERMANENT work authorization required
  W CPT / F-1 students explicitly welcome
  S no visa sponsorship / must not need sponsorship now or in future / authorized for any employer
  V the company sponsors visas
  M master's / graduate students can apply (BS/MS, "BS or higher", graduate level, all levels)
  U only undergraduates / bachelor's students (pursuing a Bachelor's, rising junior/senior, undergraduate program)
  D only PhD students
  B a bachelor's / undergraduate is only preferred
  F a graduation window or date that includes May 2028 (±3 months)
  X a graduation window or date that excludes May 2028
  N none of these
python scripts/teacher_label.py IN.json OUT.json   (IN = list of sentences; OUT = {sentence: codes}; resumable)
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radar import llm  # noqa: E402

PROMPT = """Label each sentence from US internship job descriptions for ONE applicant: a master's student (MS ECE) on an \
F-1 visa who needs CPT, graduating May 2028, not a US citizen, no green card, no clearance.
Codes (use all that apply, else N):
C = requires US citizenship, green card, U.S. person (ITAR/EAR), a security clearance, or another country's citizenship
R = says F-1/CPT/OPT/student-visa holders are not eligible / don't meet requirements, or requires PERMANENT work authorization
W = says CPT/F-1 students can apply
S = no visa sponsorship, must not need sponsorship now or in the future, or must be authorized for any employer
V = the company will sponsor visas
M = master's or graduate students can apply (BS/MS, BS or higher, graduate internship, all degree levels)
U = only undergraduate/bachelor's students (pursuing a Bachelor's, rising junior/senior, undergraduate program)
D = only PhD students
B = a bachelor's/undergraduate degree is only preferred
F = a graduation window/date that includes May 2028 (±3 months)
X = a graduation window/date that excludes May 2028
N = none
A company that only MAY decline non-U.S. persons is N, not C. Dates not about the applicant's graduation are N.
Reply with one line per sentence, nothing else: <id>:<codes>   e.g.  4:SM"""

CODES = set("CRWSVMUDBFXN")


def label(batch, key, budget):
    user = "\n".join(f"<s id={i}>{s}</s>" for i, s in batch)
    body = {"model": "openai/gpt-oss-120b", "temperature": 0, "reasoning_effort": "medium",
            "max_tokens": 1200 + 12 * len(batch),
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]}
    for attempt in range(5):
        budget.before(body["max_tokens"] + len(user) // 3 + 500)
        try:
            raw, headers = llm._post(body, key)
            budget.after(raw.get("usage") or {}, headers)
            text = raw["choices"][0]["message"]["content"] or ""
            out = {}
            for m in re.finditer(r"(\d+)\s*:\s*([A-Z]+)", text):
                codes = "".join(sorted(set(m.group(2)) & CODES))
                if codes:
                    out[int(m.group(1))] = codes
            return out
        except llm.urllib.error.HTTPError as e:
            msg = (e.read() or b"").decode("utf-8", "ignore")
            if "per day" in msg:
                raise SystemExit("daily token limit reached; progress is saved, run again later to continue")
            time.sleep(min(65, 8 * (attempt + 1)))
        except Exception:
            time.sleep(3)
    return {}


def main():
    src, dst = sys.argv[1], sys.argv[2]
    sents = json.load(open(src))
    done = json.load(open(dst)) if os.path.exists(dst) else {}
    key = os.environ["GROQ_API_KEY"]
    budget = llm.Budget(run_tokens=10**9, run_requests=10**6)
    todo = [s for s in sents if s not in done]
    for i in range(0, len(todo), 25):
        chunk = todo[i:i + 25]
        got = label(list(enumerate(chunk)), key, budget)
        for j, s in enumerate(chunk):
            if j in got:
                done[s] = got[j]
        json.dump(done, open(dst, "w"), indent=0)
        print(f"{len(done)}/{len(sents)} labeled  {budget.report()}", flush=True)


if __name__ == "__main__":
    main()
