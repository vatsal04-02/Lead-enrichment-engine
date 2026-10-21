"""Niche and India *evidence* (keyword and string matching only). Hints for a human reviewer, never a classification by a model
and never a claim: "none" means no keyword was found, "unknown" means there was no text to look at."""
from __future__ import annotations

import re
from typing import Iterable, Optional

STOP = {"agency", "agencies", "company", "companies", "firm", "firms", "services", "service", "solutions", "india", "indian",
        "business", "businesses", "b2b", "and", "the", "for", "of", "in", "a", "an", "provider", "providers", "consultancy"}

# Synonyms are *search/match hints* for the niches FlowHQ targets; extend per run with --keyword.
SYNONYMS = {
    "automation": ["automation", "automate", "workflow", "workflows", "rpa", "robotic process", "n8n", "zapier", "make.com",
                   "integromat", "process automation", "no-code", "low-code", "integration"],
    "ai": ["ai", "artificial intelligence", "machine learning", "llm", "llms", "generative ai", "genai", "gpt", "chatbot",
           "chatbots", "ai agent", "ai agents", "agentic", "conversational ai", "nlp", "copilot"],
    "chatbot": ["chatbot", "chatbots", "conversational", "voicebot", "whatsapp bot"],
    "crm": ["crm", "hubspot", "salesforce", "zoho"],
    "marketing": ["marketing automation", "lead generation", "outreach"],
}


def expand_keywords(niches: Iterable[str], extra: Iterable[str] = ()) -> list[str]:
    terms: list[str] = []
    for n in niches:
        n = (n or "").lower()
        if n.strip() and n.strip() not in terms:
            terms.append(n.strip())
        for w in re.findall(r"[a-z0-9][a-z0-9.+-]*", n):
            if w in STOP or len(w) < 2:
                continue
            for t in SYNONYMS.get(w, [w]):
                if t not in terms:
                    terms.append(t)
    for e in extra:
        e = (e or "").strip().lower()
        if e and e not in terms:
            terms.append(e)
    return terms


def _compile(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")


def find_terms(text: str, keywords: Iterable[str]) -> list[str]:
    low = (text or "").lower()
    return [k for k in keywords if _compile(k).search(low)]


def hit_signal(hit, keywords: Iterable[str]) -> str:
    """Keyword terms present in what the SOURCE said about the business (name/evidence/industry). A weak, name-level hint."""
    text = " ".join(x for x in (hit.name, hit.evidence, hit.industry) if x)
    return ", ".join(find_terms(text, keywords))


def niche_match(texts: Iterable[Optional[str]], keywords: list[str]) -> tuple[str, str]:
    """-> (strong|weak|none|unknown, matched terms). strong = >=2 distinct terms in the company's own words."""
    joined = " ".join(t for t in texts if t)
    if not joined.strip():
        return "unknown", ""
    hits = find_terms(joined, keywords)
    return ("strong" if len(hits) >= 2 else "weak" if hits else "none"), ", ".join(hits)


INDIAN_REGIONS = [
    "india", "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
    "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland", "odisha",
    "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand", "west bengal", "delhi",
    "new delhi", "chandigarh", "puducherry", "bengaluru", "bangalore", "mumbai", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad",
    "gurugram", "gurgaon", "noida", "jaipur", "kochi", "cochin", "indore", "surat", "lucknow", "coimbatore", "thiruvananthapuram",
    "nagpur", "visakhapatnam", "bhubaneswar", "vadodara", "mohali"]
_REGION_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(r) for r in sorted(INDIAN_REGIONS, key=len, reverse=True)) + r")(?![a-z])")


def india_evidence(domain: str, phone: Optional[str], address: Optional[str], source_location: Optional[str] = None) -> tuple[str, str]:
    """-> ("yes"|"unconfirmed", signals). 'yes' needs a signal from the company's own data; absence is NOT evidence against."""
    sig = []
    d = (domain or "").lower()
    if d.endswith(".in") or d.endswith(".co.in") or d.endswith(".org.in") or d.endswith(".net.in") or d.endswith(".ac.in"):
        sig.append("tld_in")
    digits = re.sub(r"[^\d+]", "", phone or "")
    if digits.startswith("+91") or digits.startswith("0091"):
        sig.append("phone_+91")
    if address:
        m = _REGION_RE.search(address.lower())
        if m:
            sig.append(f"address_mentions:{m.group(1)}")
    soft = []
    if source_location and _REGION_RE.search(source_location.lower()):
        soft.append(f"source_says:{_REGION_RE.search(source_location.lower()).group(1)}")
    return ("yes" if sig else "unconfirmed"), ", ".join(sig + soft)
