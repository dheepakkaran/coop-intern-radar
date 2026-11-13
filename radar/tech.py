"""Which jobs are tech jobs (AI/ML, Data, Software)?

A job is kept when its TITLE has a tech role word, or, when the title doesn't, its JOB DESCRIPTION has
at least 4 different strong tech terms including one core term (so a marketing or operations JD that says
"use AI tools, Tableau, Power BI" doesn't count).

Vocabulary built 2026-11-13 from ~12,500 intern titles in the SimplifyJobs lists (AI/ML/Data + Software
categories, most common 1- and 2-word phrases) plus new LLM-era titles (inference, forward deployed, evals,
post-training, agents, MLOps/LLMOps ...).
"""
import re

# ---- Title vocabulary, per field (first match wins: AI/ML, then Data, then Software) --------------------
TITLE_GROUPS = [
    ("AI/ML", [
        r"\bai\b", r"\ba\.i\.", r"artificial intelligence", r"\bai/ml\b", r"\bml\b", r"machine learning", r"deep learning",
        r"\bllms?\b", r"large language", r"gen ?ai\b", r"generative", r"agentic", r"\bai agents?\b",
        r"agents? (engineer|developer|engineering)", r"\bnlp\b", r"natural language", r"computer vision",
        r"\bvision\b", r"perception", r"reinforcement learning", r"\brl\b", r"robot learning", r"embodied",
        r"foundation models?", r"multimodal", r"inference", r"post[- ]?training", r"pre[- ]?training",
        r"fine[- ]?tuning", r"\bevals?\b", r"model (evaluation|behavior|behaviour|training|serving|optimization)",
        r"alignment", r"ai safety", r"interpretability", r"applied scien", r"research scien", r"research engineer",
        r"\bml ?ops\b", r"\bllm ?ops\b", r"\bai ?ops\b", r"prompt engineer", r"autonomy", r"autonomous",
        r"speech (recognition|ai)", r"recommend(er|ation) systems?", r"\branking\b", r"search relevance",
        r"neural", r"transformer", r"\bgpu\b", r"\bcuda\b", r"kernel engineer", r"ml (systems|infra)",
        r"forward[- ]deployed", r"\bfdes?\b", r"applied ai", r"ai/data", r"intelligent systems",
        r"knowledge graph", r"\brag\b", r"retrieval",
    ]),
    ("Data", [
        r"\bdata\b", r"analytics", r"business intelligence", r"\bbi\b", r"data scien", r"statistic",
        r"\bsql\b", r"database", r"big data", r"decision scien", r"quantitative (analyst|researcher|research)",
        r"\bgis\b", r"geospatial", r"bioinformatic", r"computational", r"\betl\b", r"data warehouse",
    ]),
    ("Software", [
        r"software", r"\bswe\b", r"\bsde\b", r"\bsdet\b", r"developer", r"programmer", r"programming",
        r"full[- ]?stack", r"front[- ]?end", r"back[- ]?end", r"\bweb\b", r"\bmobile\b", r"\bios\b", r"android",
        r"devops", r"dev ops", r"\bsre\b", r"site reliability", r"\bcloud\b", r"platform engineer",
        r"infrastructure engineer", r"distributed systems", r"systems software", r"embedded software",
        r"\bcompilers?\b", r"computer science", r"computer engineer", r"technical staff", r"\bgame(s|play)? (dev|eng|program)",
        r"test automation", r"qa automation", r"automation engineer", r"\brpa\b", r"security engineer",
        r"application security", r"product security", r"cyber ?security", r"\bapis?\b", r"\bjava\b", r"python",
        r"c\+\+", r"\.net\b", r"salesforce developer", r"robotics software", r"simulation software",
        r"solutions engineer", r"application (engineer|development|developer)", r"\bapps?\b developer",
    ]),
]
TITLE_RX = [(field, re.compile("|".join(pats), re.I)) for field, pats in TITLE_GROUPS]

# Titles that look techy but aren't the roles we want.
TITLE_NOT = re.compile(
    r"data entry|real estate develop|land develop|business develop|fund ?develop|talent develop|"
    r"leadership develop|organi[sz]ational develop|professional develop|community develop|economic develop|"
    r"(insurance|travel|leasing|customer service|sales|real estate) agent|patient care|nurse|nursing|"
    r"teaching assistant|\bweb ?(content|copy|writer|editor)|social media", re.I)

# ---- JD vocabulary ----------------------------------------------------------------------------------------
JD_CORE = [
    "machine learning", "deep learning", "large language model", "llm", "generative ai", "nlp", "natural language processing",
    "computer vision", "reinforcement learning", "pytorch", "tensorflow", "jax", "scikit-learn", "neural network",
    "data science", "data engineering", "data pipeline", "software development", "software engineering",
    "full stack", "backend", "frontend", "distributed systems", "inference", "fine-tuning", "rag",
    "retrieval-augmented", "mlops", "cuda", "transformer",
]
JD_SUPPORT = [
    "data analytics", "python", "sql", "java", "c++", "javascript", "typescript", "react", "node.js", "golang", "rust", "scala",
    "spark", "kafka", "airflow", "dbt", "snowflake", "databricks", "etl", "tableau", "power bi", "pandas", "numpy",
    "statistics", "kubernetes", "docker", "aws", "gcp", "azure", "git", "api", "microservices", "linux",
    "prompt engineering", "vector database", "langchain", "hugging face", "agents", "ci/cd",
]
_JD_RX = {t: re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", re.I) for t in JD_CORE + JD_SUPPORT}
JD_MIN_TERMS = 4

TAG = re.compile(r"<[^>]+>")


def title_field(title: str) -> str | None:
    """'AI/ML' | 'Data' | 'Software' if the title is a tech role, else None."""
    if not title or TITLE_NOT.search(title):
        return None
    for field, rx in TITLE_RX:
        if rx.search(title):
            return field
    return None


def jd_terms(text: str | None) -> list[str] | None:
    """Strong tech terms found in a job description. None = no description available."""
    if text is None:
        return None
    text = TAG.sub(" ", text)
    return [t for t, rx in _JD_RX.items() if rx.search(text)]


def jd_is_tech(terms: list[str] | None) -> bool:
    return bool(terms) and len(terms) >= JD_MIN_TERMS and any(t in JD_CORE for t in terms)


def jd_field(terms: list[str]) -> str:
    s = set(terms)
    if s & {"machine learning", "deep learning", "large language model", "llm", "generative ai", "nlp",
            "natural language processing", "computer vision", "reinforcement learning", "pytorch", "tensorflow",
            "jax", "neural network", "inference", "fine-tuning", "rag", "retrieval-augmented", "mlops", "cuda",
            "transformer"}:
        return "AI/ML"
    if s & {"data science", "data engineering", "data pipeline", "data analytics", "tableau", "power bi", "etl"}:
        return "Data"
    return "Software"


def classify(title: str, category: str | None = None, terms: list[str] | None = None) -> tuple[str, str] | None:
    """(field, why) for a tech job, else None. why = 'title' | 'JD'.

    `category` (SimplifyJobs' own tag) is ignored on purpose: it put e.g. forestry and customer-letter roles
    under AI/ML. Only the title and the JD decide."""
    f = title_field(title)
    if f:
        return f, "title"
    if TITLE_NOT.search(title or ""):
        return None
    if jd_is_tech(terms):
        return jd_field(terms), "JD"
    return None
