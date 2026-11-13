"""Tag a job: co-op or intern, field, place."""
import re

COOP = re.compile(r"\bco[- ]?op\b|\bco-operative\b|\bcooperative education\b", re.I)
# Non-US job titles that slip through: French/German internship words and gender tags.
FOREIGN_TITLE = re.compile(r"\(?\b[mwfd]/[mwfd](/[mwfd])?\)?|\b(h/f|f/h)\b|werkstudent|praktikant|praktikum|stagiaire|"
                           r"\bstage\b|alternan(ce|t)|\bbecario\b|\bpr[aá]cticas\b|year in industry", re.I)
INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|\bapprentice(ship)?\b|\bsummer (analyst|associate)s?\b", re.I)

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
# ---- US-only filter -------------------------------------------------------
STATE_CODES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
               "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR").split()
STATE_NAMES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
               "illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
               "mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
               "north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
               "south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
               "district of columbia|puerto rico")
US_CODE = re.compile(r"(?:,|\b)\s*(" + "|".join(STATE_CODES) + r")\b(?!\.?\s*,?\s*(?:canada|can)\b)")
US_WORDS = re.compile(r"\b(united states|u\.s\.a?\.?|usa|us|nyc|sf|bay area|silicon valley|" + STATE_NAMES + r")\b", re.I)
CA_PROVINCE = re.compile(r",\s*(ON|BC|QC|AB|MB|NS|NB|NL|SK|PE)\b")
COUNTRIES = (
    "canada|can|uk|united kingdom|great britain|england|scotland|wales|ireland|india|germany|deutschland|france|spain|"
    "italy|portugal|netherlands|holland|belgium|luxembourg|poland|switzerland|austria|sweden|denmark|norway|finland|"
    "iceland|estonia|latvia|lithuania|czech republic|czechia|slovakia|slovenia|croatia|serbia|romania|bulgaria|"
    "hungary|greece|cyprus|malta|ukraine|russia|belarus|georgia \\(country\\)|armenia|turkey|t[uü]rkiye|israel|"
    "palestine|jordan|lebanon|saudi arabia|qatar|kuwait|bahrain|oman|uae|united arab emirates|dubai|abu dhabi|"
    "egypt|morocco|tunisia|algeria|nigeria|ghana|kenya|ethiopia|rwanda|uganda|tanzania|south africa|"
    "pakistan|bangladesh|sri lanka|nepal|singapore|malaysia|indonesia|philippines|vietnam|thailand|cambodia|"
    "china|hong kong|macau|taiwan|japan|korea|south korea|mongolia|kazakhstan|uzbekistan|australia|new zealand|"
    "mexico|guatemala|honduras|el salvador|nicaragua|costa rica|panama|cuba|jamaica|dominican republic|"
    "bahamas|barbados|trinidad|cayman|bermuda|colombia|venezuela|ecuador|peru|bolivia|chile|argentina|"
    "uruguay|paraguay|brazil|brasil")
FOREIGN_PLACES = (
    "europe|emea|apac|latam|asia|africa|toronto|vancouver|montr[eé]al|ottawa|waterloo|calgary|edmonton|winnipeg|"
    "mississauga|markham|quebec|london|dublin|berlin|munich|m[uü]nchen|hamburg|frankfurt|paris|amsterdam|madrid|"
    "barcelona|lisbon|milan|rome|stockholm|copenhagen|oslo|helsinki|warsaw|krak[oó]w|prague|vienna|zurich|z[uü]rich|"
    "geneva|bangalore|bengaluru|hyderabad|chennai|pune|mumbai|delhi|gurgaon|gurugram|noida|tokyo|osaka|seoul|"
    "shanghai|beijing|shenzhen|taipei|sydney|melbourne|brisbane|tel aviv|istanbul|s[aã]o paulo|mexico city|"
    "guadalajara|bogot[aá]|buenos aires|santiago|lima|manila|jakarta|kuala lumpur|bangkok|ho chi minh|hanoi")
NON_US = re.compile(r"\b(" + COUNTRIES + "|" + FOREIGN_PLACES + r")\b", re.I)
US_COUNTRY = {"us", "usa", "united states", "united states of america", "u.s.", "u.s.a."}


COUNTRY_WORDS = re.compile(r"\b(canada|can|uk|united kingdom|england|india|germany|france|ireland|mexico|china|japan)\b", re.I)
US_STRICT = re.compile(r",\s*(" + "|".join(STATE_CODES) + r")\s*(?:,\s*(?:us|usa|united states)\s*)?(?:\d{5})?\s*$", re.I)


def loc_country(s: str) -> str | None:
    """'US', 'X' (outside the US) or None (can't tell, e.g. 'Remote')."""
    s = s.strip()
    m = re.match(r"^([A-Z]{2})-[A-Z0-9]{2,3}-", s)  # Workday site codes like "MX-BCN-MEXICALI-238", "US-CA-..."
    if m:
        return "US" if m.group(1) == "US" else "X"
    if re.search(r"\bnew mexico\b", s, re.I):
        return "US"
    if CA_PROVINCE.search(s):
        return "X"
    if US_STRICT.search(s) and not COUNTRY_WORDS.search(s):  # 'Melbourne, FL', 'Carlsbad, Ca'
        return "US"
    if NON_US.search(s):
        # 'Remote in USA or Canada' style strings: the US mention wins.
        return "US" if re.search(r"\b(united states|usa|u\.s\.)\b", s, re.I) else "X"
    if US_CODE.search(s) or US_WORDS.search(s):
        return "US"
    return None


def is_us(locations: list[str], countries: list[str] | None = None) -> bool:
    """Keep a job if any place is in the US, or if no place says where it is (e.g. just 'Remote')."""
    cs = {c.strip().lower() for c in (countries or []) if c}
    if cs & US_COUNTRY:
        return True
    tags = [loc_country(l) for l in locations if l and l.strip()]
    if "US" in tags:
        return True
    if cs and not (cs & US_COUNTRY):
        return False
    return not any(t == "X" for t in tags)


TERM = re.compile(r"\b(spring|summer|fall|autumn|winter)\b[\s,'-]*(20\d\d|'?\d\d)?", re.I)


def terms_from_title(title: str) -> list[str]:
    out = []
    for season, yr in TERM.findall(title):
        season = "Fall" if season.lower() == "autumn" else season.capitalize()
        yr = yr.lstrip("'")
        out.append(f"{season} {('20' + yr) if len(yr) == 2 else yr}".strip())
    return sorted(set(out))


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
    return tags


def is_early_career(title: str, from_intern_feed: bool) -> bool:
    """Intern feeds are already filtered; other sources must match the title."""
    return from_intern_feed or bool(COOP.search(title) or INTERN.search(title))
