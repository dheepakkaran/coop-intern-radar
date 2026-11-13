"""Tag a job: co-op or intern, field, place."""
import re

COOP = re.compile(r"\bco-?op\b|\bcoop\b|\bco-operative\b|\bcooperative education\b", re.I)
INTERN = re.compile(r"\bintern(ship)?s?\b|\bapprentice|\bplacement\b|\bstudent\b|\bsummer analyst\b", re.I)

# Off-season terms are where co-ops live.
OFF_SEASON = ("Spring", "Fall", "Winter")

FIELDS = [
    ("AI/ML/Data", re.compile(r"\b(ai|ml|machine learning|data|analytics?|scien|nlp|llm|vision|deep learning|research)\b", re.I)),
    ("Quant", re.compile(r"\b(quant|trading|trader)\b", re.I)),
    ("Hardware", re.compile(r"\b(hardware|electrical|asic|fpga|firmware|embedded|robotics?|mechanical|silicon|chip|rf)\b", re.I)),
    ("Product", re.compile(r"\b(product|program manag|design)\b", re.I)),
    ("Software", re.compile(r"\b(software|engineer|developer|swe|backend|frontend|full ?stack|devops|cloud|security)\b", re.I)),
]

FIELD_ALIASES = {
    "Software Engineering": "Software",
    "Data Science, AI & Machine Learning": "AI/ML/Data",
    "Quantitative Finance": "Quant",
    "Product Management": "Product",
}

BOSTON = re.compile(r"\b(boston|cambridge, ma|somerville|waltham|burlington, ma|lexington, ma|woburn|bedford, ma|"
                    r"framingham|quincy, ma|medford|wilmington, ma|andover|lowell|worcester|, ma\b|massachusetts)", re.I)
REMOTE = re.compile(r"\bremote\b", re.I)
CANADA = re.compile(r"\b(canada|toronto|vancouver|montr[eé]al|ottawa|waterloo|calgary|mississauga|"
                    r"(on|bc|qc|ab|mb|ns), can\b|, (on|bc|qc|ab)\b)", re.I)


def kind(title: str, terms: list[str]) -> str:
    """'co-op' if the title says so or the term is off-season, else 'intern'."""
    if COOP.search(title):
        return "co-op"
    if any(t.split()[0] in OFF_SEASON for t in terms if t and t != "N/A"):
        return "co-op"
    return "intern"


def field(title: str, given: str | None) -> str:
    if given:
        return FIELD_ALIASES.get(given, given)
    for name, rx in FIELDS:
        if rx.search(title):
            return name
    return "Other"


def places(locations: list[str]) -> list[str]:
    tags = []
    text = " | ".join(locations)
    if BOSTON.search(text):
        tags.append("Boston/MA")
    if REMOTE.search(text):
        tags.append("Remote")
    if locations and all(CANADA.search(l) for l in locations):
        tags.append("Canada")
    return tags


def is_early_career(title: str, from_intern_feed: bool) -> bool:
    """Intern feeds are already filtered; other sources must match the title."""
    return from_intern_feed or bool(COOP.search(title) or INTERN.search(title))
