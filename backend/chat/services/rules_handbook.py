# =============================================================================
# LEGACY / REFERENCE ONLY — not imported by orchestrator or RAG.
# Company policy answers are served only from the knowledge-base (Qdrant + RAG).
# This module remains in the repo for historical keyword-matching logic if needed.
# =============================================================================
#
# """Real Estate Company Employee Rules & Regulations handbook.
#
# Static, in-process source of truth for company rules.
#
# Usage from the orchestrator:
#
#     from chat.services.rules_handbook import answer_rules_query
#
#     pack = answer_rules_query(user_message)
#     # pack = {"mode": "full"|"section"|"toc", "text": "...", "matched": [1,4,...]}
#
# The matcher is deterministic (keyword scoring); no LLM call is required so
# the bot can answer rules questions even when the LLM is offline.
# """

from __future__ import annotations

import re
from typing import Any

HANDBOOK_TITLE = "Real Estate Company Employee Rules & Regulations Handbook"
HANDBOOK_SUBTITLE = "Enterprise-Level Standard Policy for All Employees"
HANDBOOK_INTRO = (
    "This handbook is designed for a professional real estate company including "
    "Real Estate Developers, Construction Firms, Property Management Companies, "
    "Brokerage Firms, and Housing & Commercial Property Companies.\n\n"
    "Purpose: professionalism, discipline, legal compliance, operational efficiency, "
    "customer trust, and workplace safety."
)


# Each section: number, title, keywords (used for matching), body (markdown).
# Keep keywords lowercase. Title words are added to keywords automatically.
RULES_HANDBOOK: list[dict[str, Any]] = [
    {
        "number": 1,
        "title": "Company Core Values",
        "keywords": [
            "core values", "values", "principles", "ethics", "integrity",
            "professionalism", "accountability", "transparency", "respect",
            "confidentiality", "teamwork", "customer-first", "customer first",
        ],
        "body": (
            "Every employee must follow these principles:\n"
            "- Integrity\n"
            "- Professionalism\n"
            "- Accountability\n"
            "- Transparency\n"
            "- Respect\n"
            "- Confidentiality\n"
            "- Teamwork\n"
            "- Customer-first mindset\n\n"
            "Employees must represent the company ethically both inside and outside "
            "the workplace."
        ),
    },
    {
        "number": 2,
        "title": "Employment Policy",
        "keywords": [
            "employment", "joining", "probation", "working hours", "office timing",
            "attendance", "biometric", "rfid", "erp login", "buddy punching",
            "late", "lateness", "punctuality", "office hours", "schedule",
        ],
        "body": (
            "**2.1 Joining Requirements**\n"
            "Every employee must submit:\n"
            "- National ID / Passport\n"
            "- Educational documents\n"
            "- Experience certificates\n"
            "- Bank information\n"
            "- Emergency contact\n"
            "- Signed employment agreement\n\n"
            "False information may result in immediate termination.\n\n"
            "**2.2 Probation Period**\n"
            "- Standard probation: 3–6 months\n"
            "- Performance evaluation mandatory\n"
            "- Confirmation depends on discipline, attendance, performance, behavior\n\n"
            "**2.3 Working Hours**\n"
            "- Standard office timing: 9:00 AM – 6:00 PM\n"
            "- 6 working days/week (or company-defined schedule)\n"
            "- Employees must arrive on time.\n"
            "- Late attendance beyond allowed limits may result in penalties.\n"
            "- Unauthorized early departure prohibited.\n\n"
            "**2.4 Attendance Policy**\n"
            "Attendance methods: biometric, mobile GPS, RFID card, ERP login tracking.\n"
            "Violations:\n"
            "- Buddy punching prohibited\n"
            "- Fake attendance prohibited\n"
            "- Repeated lateness may result in warnings"
        ),
    },
    {
        "number": 3,
        "title": "Dress Code & Professional Appearance",
        "keywords": [
            "dress code", "dress", "attire", "appearance", "uniform", "id card",
            "ppe", "helmet", "safety shoes", "reflective vest", "gloves",
            "construction site dress", "site dress",
            "পোশাক", "poshak", "uniform code",
        ],
        "body": (
            "Employees must maintain professional appearance.\n\n"
            "**Mandatory Rules**\n"
            "- Clean and formal attire\n"
            "- ID card visible during office hours\n"
            "- Safety gear mandatory on construction sites\n"
            "- Offensive or inappropriate clothing prohibited\n\n"
            "**Construction Site Requirements – Mandatory PPE**\n"
            "- Helmet\n"
            "- Safety shoes\n"
            "- Reflective vest\n"
            "- Gloves (when required)\n\n"
            "Failure to follow safety dress code may result in site removal."
        ),
    },
    {
        "number": 4,
        "title": "Code of Conduct",
        "keywords": [
            "code of conduct", "conduct", "behavior", "behaviour", "respect colleagues",
            "professional behavior", "workplace conflict", "abusive language",
            "harassment", "bullying", "threats", "discrimination", "violence",
            "intimidation",
        ],
        "body": (
            "Employees must:\n"
            "- Respect colleagues\n"
            "- Maintain professionalism\n"
            "- Avoid abusive language\n"
            "- Avoid workplace conflict\n"
            "- Follow management instructions\n"
            "- Maintain ethical behavior\n\n"
            "Strictly prohibited:\n"
            "- Harassment\n"
            "- Bullying\n"
            "- Threats\n"
            "- Discrimination\n"
            "- Physical violence\n"
            "- Workplace intimidation"
        ),
    },
    {
        "number": 5,
        "title": "Anti-Harassment & Workplace Respect Policy",
        "keywords": [
            "harassment", "anti-harassment", "sexual harassment", "mental harassment",
            "verbal abuse", "gender discrimination", "religious discrimination",
            "racism", "cyberbullying", "zero tolerance", "respect policy",
        ],
        "body": (
            "The company maintains zero tolerance for:\n"
            "- Sexual harassment\n"
            "- Mental harassment\n"
            "- Verbal abuse\n"
            "- Gender discrimination\n"
            "- Religious discrimination\n"
            "- Racism\n"
            "- Cyberbullying\n\n"
            "Any complaint will be investigated confidentially.\n\n"
            "Violation may result in:\n"
            "- Suspension\n"
            "- Termination\n"
            "- Legal action"
        ),
    },
    {
        "number": 6,
        "title": "Confidentiality & Data Protection",
        "keywords": [
            "confidentiality", "data protection", "client data", "property pricing",
            "contracts", "financial data", "employee records", "business strategy",
            "source code", "vendor agreements", "data leak", "data breach",
            "leak", "nda",
        ],
        "body": (
            "Employees must protect company information.\n\n"
            "**Confidential Information Includes**\n"
            "- Client data\n"
            "- Property pricing\n"
            "- Contracts\n"
            "- Financial data\n"
            "- Employee records\n"
            "- Business strategy\n"
            "- Source code / software\n"
            "- Vendor agreements\n\n"
            "**Strict Rules** – Employees may NOT:\n"
            "- Share confidential data externally\n"
            "- Copy company data without permission\n"
            "- Use company data for personal benefit\n"
            "- Leak client information\n\n"
            "Data breach may result in immediate termination and legal action."
        ),
    },
    {
        "number": 7,
        "title": "IT & System Usage Policy",
        "keywords": [
            "it policy", "system usage", "computer use", "pirated software",
            "usb", "hacking", "password", "2fa", "two factor", "security",
            "downloading", "illegal content",
        ],
        "body": (
            "**Acceptable Use**\n"
            "Employees may use company systems only for official work.\n\n"
            "**Strictly Prohibited**\n"
            "- Pirated software\n"
            "- Unauthorized USB devices\n"
            "- Hacking attempts\n"
            "- Sharing passwords\n"
            "- Downloading illegal content\n"
            "- Bypassing security systems\n\n"
            "**Password Policy**\n"
            "- Strong passwords mandatory\n"
            "- Password sharing prohibited\n"
            "- 2FA required where applicable"
        ),
    },
    {
        "number": 8,
        "title": "Email & Communication Policy",
        "keywords": [
            "email", "communication", "official email", "abusive emails",
            "impersonate", "cc", "suspicious email", "phishing",
        ],
        "body": (
            "Official communication must remain professional.\n\n"
            "**Employees Must Not**\n"
            "- Send abusive emails\n"
            "- Spread misinformation\n"
            "- Share confidential documents without approval\n"
            "- Impersonate management\n\n"
            "**Email Rules**\n"
            "- Use official email for official work\n"
            "- Maintain professional tone\n"
            "- Avoid unnecessary CCs\n"
            "- Report suspicious emails immediately"
        ),
    },
    {
        "number": 9,
        "title": "Leave Policy",
        "keywords": [
            "leave", "leave policy", "casual leave", "sick leave", "annual leave",
            "maternity", "paternity", "emergency leave", "lwop", "leave without pay",
            "vacation", "pto", "time off", "holiday",
            "ছুটি", "chuti", "chhuti", "অসুস্থতা",
        ],
        "body": (
            "**Types of Leave**\n"
            "- Casual Leave\n"
            "- Sick Leave\n"
            "- Annual Leave\n"
            "- Maternity / Paternity Leave\n"
            "- Emergency Leave\n"
            "- Leave Without Pay\n\n"
            "**Leave Rules**\n"
            "- Leave request must be submitted through ERP / system\n"
            "- Manager approval mandatory\n"
            "- Medical certificate required for extended sick leave\n"
            "- Unauthorized absence considered misconduct"
        ),
    },
    {
        "number": 10,
        "title": "Salary & Compensation Policy",
        "keywords": [
            "salary", "compensation", "payroll", "overtime", "tax",
            "salary confidentiality", "fake overtime",
            "বেতন", "beton", "মাইনে",
        ],
        "body": (
            "**Salary Rules**\n"
            "- Salary processed monthly\n"
            "- Overtime policy company-defined\n"
            "- Tax deductions applicable\n"
            "- Salary confidentiality required\n\n"
            "**Prohibited**\n"
            "- Manipulating payroll data\n"
            "- False expense claims\n"
            "- Fake overtime submission\n\n"
            "Fraudulent claims may result in termination."
        ),
    },
    {
        "number": 11,
        "title": "Expense & Reimbursement Policy",
        "keywords": [
            "expense", "reimbursement", "bills", "receipts", "fake expense",
            "business expense", "reimburse",
            "খরচ", "kharcha", "khoroch", "taka", "টাকা",
        ],
        "body": (
            "Employees must submit:\n"
            "- Valid bills\n"
            "- Receipts\n"
            "- Approval references\n\n"
            "Fake expenses are strictly prohibited.\n\n"
            "**Reimbursement Rules**\n"
            "- Business expenses only\n"
            "- Manager approval mandatory\n"
            "- Late submissions may be rejected"
        ),
    },
    {
        "number": 12,
        "title": "Sales & Client Handling Rules",
        "keywords": [
            "sales", "client handling", "crm", "leads", "lead entry",
            "private deals", "side commission", "brokerage", "pricing manipulation",
            "marketing",
        ],
        "body": (
            "Applicable for Sales Executives, CRM Team, and Marketing Team.\n\n"
            "**Mandatory Rules**\n"
            "- All leads must be entered into CRM\n"
            "- No private deals with clients\n"
            "- False promises prohibited\n"
            "- Pricing manipulation prohibited\n"
            "- Client communication must be documented\n\n"
            "**Strictly Prohibited**\n"
            "- Accepting unauthorized cash\n"
            "- Side commissions\n"
            "- Personal brokerage using company clients"
        ),
    },
    {
        "number": 13,
        "title": "Property Site Rules",
        "keywords": [
            "property site", "site rules", "construction site", "ppe",
            "safety briefing", "incident reporting", "alcohol on site",
            "drugs on site", "machinery", "site supervisor", "engineer", "worker",
            "contractor",
        ],
        "body": (
            "Applicable for Engineers, Site Supervisors, Contractors, and Workers.\n\n"
            "**Mandatory Safety Rules**\n"
            "- PPE mandatory\n"
            "- Safety briefing mandatory\n"
            "- Incident reporting mandatory\n"
            "- Unsafe work prohibited\n\n"
            "**Strictly Prohibited**\n"
            "- Alcohol / drugs on site\n"
            "- Unsafe machinery operation\n"
            "- Ignoring safety protocols"
        ),
    },
    {
        "number": 14,
        "title": "Procurement & Vendor Policy",
        "keywords": [
            "procurement", "vendor", "purchasing", "quotation", "anti-bribery",
            "kickback", "fake invoice", "vendor favoritism", "bribery",
        ],
        "body": (
            "Employees involved in purchasing must follow:\n"
            "- Transparent quotation process\n"
            "- Approved vendor usage\n"
            "- Anti-bribery compliance\n\n"
            "**Strictly Prohibited**\n"
            "- Personal commissions\n"
            "- Kickbacks\n"
            "- Fake invoices\n"
            "- Vendor favoritism"
        ),
    },
    {
        "number": 15,
        "title": "Financial Compliance Policy",
        "keywords": [
            "financial compliance", "accounts", "finance", "cash handling",
            "audit", "voucher", "dual approval", "financial fraud",
        ],
        "body": (
            "Applicable for Accounts, Finance, and Management.\n\n"
            "**Rules**\n"
            "- Every transaction must be documented\n"
            "- Cash handling limits enforced\n"
            "- Unauthorized payments prohibited\n\n"
            "**Fraud Prevention**\n"
            "- Dual approval system\n"
            "- Audit logging\n"
            "- Voucher verification\n\n"
            "Financial fraud may result in criminal prosecution."
        ),
    },
    {
        "number": 16,
        "title": "Conflict of Interest Policy",
        "keywords": [
            "conflict of interest", "personal business", "relatives",
            "favoritism", "competing business", "disclosure",
        ],
        "body": (
            "Employees must disclose:\n"
            "- Relationships with vendors\n"
            "- Personal business conflicts\n"
            "- Financial interests affecting company work\n\n"
            "Employees may not:\n"
            "- Compete with company business\n"
            "- Misuse company influence\n"
            "- Favor relatives / vendors unfairly"
        ),
    },
    {
        "number": 17,
        "title": "Social Media Policy",
        "keywords": [
            "social media", "facebook", "linkedin", "twitter", "online reputation",
            "post", "hate speech", "company reputation online",
        ],
        "body": (
            "Employees must not:\n"
            "- Damage company reputation online\n"
            "- Leak internal information\n"
            "- Post confidential content\n"
            "- Engage in hate speech representing company identity\n\n"
            "Official company announcements require authorization."
        ),
    },
    {
        "number": 18,
        "title": "Workplace Safety Policy",
        "keywords": [
            "workplace safety", "safety", "hazards", "fire drill", "earthquake",
            "electrical safety", "medical emergency", "emergency procedure",
            "নিরাপত্তা", "durghotona", "agun",
        ],
        "body": (
            "The company prioritizes employee safety.\n\n"
            "**Employees Must**\n"
            "- Follow safety instructions\n"
            "- Report hazards immediately\n"
            "- Participate in drills\n"
            "- Use safety equipment properly\n\n"
            "Emergency procedures must be followed during:\n"
            "- Fire\n"
            "- Earthquake\n"
            "- Electrical incidents\n"
            "- Medical emergencies"
        ),
    },
    {
        "number": 19,
        "title": "Visitor & Office Security Policy",
        "keywords": [
            "visitor", "office security", "id verification", "access card",
            "security checkpoint", "restricted area", "guest",
        ],
        "body": (
            "**Rules**\n"
            "- Visitors must register\n"
            "- ID verification mandatory\n"
            "- Unauthorized access prohibited\n\n"
            "Employees may not:\n"
            "- Allow unknown persons into restricted areas\n"
            "- Share access cards\n"
            "- Bypass security checkpoints"
        ),
    },
    {
        "number": 20,
        "title": "Company Asset Policy",
        "keywords": [
            "company asset", "assets", "laptop", "phone", "vehicle",
            "software license", "office equipment", "damage", "loss",
        ],
        "body": (
            "Company assets include:\n"
            "- Laptops\n"
            "- Phones\n"
            "- Vehicles\n"
            "- Documents\n"
            "- Software licenses\n"
            "- Office equipment\n\n"
            "**Rules**\n"
            "- Proper usage mandatory\n"
            "- Damage / loss must be reported immediately\n"
            "- Personal use restrictions may apply"
        ),
    },
    {
        "number": 21,
        "title": "Performance Management Policy",
        "keywords": [
            "performance", "kpi", "productivity", "evaluation", "appraisal",
            "performance review", "pip", "improvement plan",
        ],
        "body": (
            "Employees are evaluated based on:\n"
            "- Productivity\n"
            "- Quality of work\n"
            "- Discipline\n"
            "- Teamwork\n"
            "- KPI achievement\n\n"
            "Repeated poor performance may lead to:\n"
            "- Warning\n"
            "- PIP (Performance Improvement Plan)\n"
            "- Termination"
        ),
    },
    {
        "number": 22,
        "title": "Disciplinary Action Policy",
        "keywords": [
            "disciplinary", "discipline", "warning", "verbal warning",
            "written warning", "final warning", "suspension", "termination",
        ],
        "body": (
            "Violation severity determines action.\n\n"
            "**Possible Actions**\n"
            "- Verbal warning\n"
            "- Written warning\n"
            "- Final warning\n"
            "- Suspension\n"
            "- Termination\n\n"
            "Serious misconduct may skip warning stages."
        ),
    },
    {
        "number": 23,
        "title": "Serious Misconduct (Immediate Termination)",
        "keywords": [
            "serious misconduct", "immediate termination", "theft", "fraud",
            "bribery", "harassment", "violence", "data leakage", "drug use",
            "fake documents", "corruption",
        ],
        "body": (
            "Examples of serious misconduct that may lead to immediate termination:\n"
            "- Theft\n"
            "- Fraud\n"
            "- Bribery\n"
            "- Harassment\n"
            "- Violence\n"
            "- Data leakage\n"
            "- Drug use at workplace\n"
            "- Fake documents\n"
            "- Corruption\n\n"
            "The company may also pursue legal action."
        ),
    },
    {
        "number": 24,
        "title": "Resignation & Exit Policy",
        "keywords": [
            "resignation", "exit", "notice period", "clearance", "final settlement",
            "handover", "return assets",
        ],
        "body": (
            "Employees must:\n"
            "- Submit formal resignation\n"
            "- Serve notice period\n"
            "- Return company assets\n"
            "- Complete clearance process\n\n"
            "Final settlement depends on:\n"
            "- Clearance approval\n"
            "- Pending liabilities\n"
            "- HR verification"
        ),
    },
    {
        "number": 25,
        "title": "Remote Work Policy",
        "keywords": [
            "remote work", "work from home", "wfh", "remote",
            "virtual meeting", "outsourcing", "বাসা থেকে কাজ", "basa theke kaj",
        ],
        "body": (
            "Employees working remotely must:\n"
            "- Maintain availability\n"
            "- Protect company data\n"
            "- Meet deadlines\n"
            "- Attend virtual meetings professionally\n\n"
            "Unauthorized outsourcing of work prohibited."
        ),
    },
    {
        "number": 26,
        "title": "ERP & System Usage Rules",
        "keywords": [
            "erp", "system usage", "data entry", "workflow approval",
            "audit log", "real-time data", "delete records",
        ],
        "body": (
            "All employees must use ERP systems properly.\n\n"
            "**Mandatory**\n"
            "- Real-time data entry\n"
            "- Accurate records\n"
            "- Approval workflow compliance\n\n"
            "**Strictly Prohibited**\n"
            "- Editing logs illegally\n"
            "- Deleting records\n"
            "- Bypassing workflow approvals\n\n"
            "All activities may be monitored and logged."
        ),
    },
    {
        "number": 27,
        "title": "Legal Compliance",
        "keywords": [
            "legal compliance", "labor law", "tax law", "property regulation",
            "anti-money laundering", "aml", "compliance",
        ],
        "body": (
            "Employees must comply with:\n"
            "- Labor law\n"
            "- Tax law\n"
            "- Property regulations\n"
            "- Anti-money laundering policies\n"
            "- Company compliance policies\n\n"
            "Violation may result in legal consequences."
        ),
    },
    {
        "number": 28,
        "title": "Ethics & Anti-Corruption Policy",
        "keywords": [
            "ethics", "anti-corruption", "bribery", "corruption",
            "illegal commission", "fraud", "whistleblower",
        ],
        "body": (
            "Zero tolerance for:\n"
            "- Bribery\n"
            "- Corruption\n"
            "- Illegal commissions\n"
            "- Fraudulent activity\n\n"
            "Employees must report suspicious behavior immediately."
        ),
    },
    {
        "number": 29,
        "title": "Customer Service Standards",
        "keywords": [
            "customer service", "client trust", "response timelines",
            "complaint", "customer support",
        ],
        "body": (
            "Employees must:\n"
            "- Respond professionally\n"
            "- Avoid misleading information\n"
            "- Maintain response timelines\n"
            "- Resolve complaints responsibly\n\n"
            "Client trust is a company priority."
        ),
    },
    {
        "number": 30,
        "title": "Employee Acknowledgment",
        "keywords": [
            "acknowledgment", "acknowledgement", "sign", "handbook agreement",
            "employee agreement",
        ],
        "body": (
            "Every employee must:\n"
            "- Read this handbook\n"
            "- Understand company policies\n"
            "- Sign acknowledgment agreement\n"
            "- Comply with all regulations\n\n"
            "Failure to follow policies may result in disciplinary action."
        ),
    },
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_section(number: int) -> dict[str, Any] | None:
    for s in RULES_HANDBOOK:
        if s["number"] == number:
            return s
    return None


def render_full_handbook() -> str:
    """Markdown-ish dump of every section. Used for 'show me all rules'."""
    parts: list[str] = [
        f"**{HANDBOOK_TITLE}**",
        f"_{HANDBOOK_SUBTITLE}_",
        "",
        HANDBOOK_INTRO,
        "",
        "---",
        "",
    ]
    for s in RULES_HANDBOOK:
        parts.append(f"### {s['number']}. {s['title']}")
        parts.append("")
        parts.append(s["body"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_section(section: dict[str, Any]) -> str:
    return f"### {section['number']}. {section['title']}\n\n{section['body']}"


def render_table_of_contents() -> str:
    lines = [f"**{HANDBOOK_TITLE} — Table of Contents**", ""]
    for s in RULES_HANDBOOK:
        lines.append(f"{s['number']}. {s['title']}")
    lines.append("")
    lines.append(
        "Ask me about any of the above (for example: \"rules about leave\", "
        "\"dress code on construction sites\", \"data confidentiality rules\")."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detection / scoring
# ---------------------------------------------------------------------------


_FULL_HANDBOOK_PATTERNS = (
    r"\ball\s+(?:the\s+)?(?:rules|regulations|policies|policy)\b",
    r"\b(?:every|each)\s+(?:rule|regulation|policy)\b",
    r"\b(?:entire|whole|complete|full)\s+(?:handbook|rules|regulations|policy|policies)\b",
    r"\b(?:give|show|send|list|read)\s+me\s+(?:all|everything)\b",
    r"\b(?:show|give|read|list)\s+(?:me\s+)?(?:the\s+)?(?:rules?\s+(?:and|&)\s+regulations?|"
    r"handbook|all\s+(?:rules?|regulations?|policies))\b",
    r"\b(?:rules?\s+(?:and|&)\s+regulations?)\b",
    r"\b(?:full|complete)\s+list\b",
    r"\bemployee\s+handbook\b",
)

_RULES_QUERY_PATTERNS = (
    r"\b(rule|rules|regulation|regulations|policy|policies|handbook|guideline|guidelines)\b",
    r"\b(allowed|prohibited|must|mustn't|forbidden|mandatory|required|may\s+not)\b",
)

_BENGALI_FULL_PATTERNS = (
    r"(সব\s*নিয়ম|সমস্ত\s*নিয়ম|সকল\s*নিয়ম|পুরো\s*হ্যান্ডবুক|সব\s*পলিসি)",
    r"\b(shob|sob|sokol)\s+(niyom|niyam|rule|rules|policy|policies|regulation)s?\b",
    r"\b(puro|pura|complete)\s+(handbook|niyom|rules?)\b",
)

_BENGALI_RULES_HINT = (
    r"(নিয়ম|বিধি|নীতি|হ্যান্ডবুক|রুলস|পলিসি)",
    r"\b(niyom|niyam|bidhi|niti|rules?|policy|policies|handbook)\b",
)


def is_rules_query(message: str) -> bool:
    """True if the message is about the rules/regulations handbook at all."""
    if not message:
        return False
    low = message.lower()
    for pat in _RULES_QUERY_PATTERNS:
        if re.search(pat, low):
            return True
    for pat in _BENGALI_RULES_HINT:
        if re.search(pat, message) or re.search(pat, low):
            return True
    return False


def wants_full_handbook(message: str) -> bool:
    """True if the user asked for the entire rulebook."""
    if not message:
        return False
    low = message.lower()
    for pat in _FULL_HANDBOOK_PATTERNS:
        if re.search(pat, low):
            return True
    for pat in _BENGALI_FULL_PATTERNS:
        if re.search(pat, message) or re.search(pat, low):
            return True
    # Bare "rules" / "regulations" with no topic words → treat as full.
    if re.fullmatch(r"\s*(rules?|regulations?|handbook|policy|policies)\s*\??\s*", low):
        return True
    return False


# Tokens we ignore when scoring relevance.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at",
        "is", "are", "be", "was", "were", "do", "does", "did", "i", "me",
        "my", "we", "us", "you", "your", "what", "which", "about", "tell",
        "show", "give", "read", "list", "all", "any", "some", "rule", "rules",
        "regulation", "regulations", "policy", "policies", "handbook",
        "company", "employee", "employees", "please", "thanks", "thank",
        "section", "part", "info", "information", "details", "detail",
    }
)


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokenize(text) if t not in _STOPWORDS and len(t) >= 3]


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _keyword_match(low: str, raw: str, kw: str) -> bool:
    """ASCII keywords use \\b boundaries with optional plural -s tolerance
    so 'expense' also matches 'expenses', 'rule' also matches 'rules', etc.
    Non-ASCII (Bengali) tokens use plain containment."""
    if not kw:
        return False
    if _is_ascii(kw):
        if " " in kw:
            return kw in low
        return bool(re.search(rf"\b{re.escape(kw)}s?\b", low))
    return kw in raw or kw in low


def score_section(message: str, section: dict[str, Any]) -> float:
    """Heuristic relevance score for one section vs. the user message."""
    if not message:
        return 0.0
    raw = message
    low = message.lower()
    score = 0.0

    for kw in section.get("keywords", []):
        if _keyword_match(low, raw, kw):
            score += 6.0 if " " in kw else 3.5

    title_tokens = [t for t in _tokenize(section["title"]) if t not in _STOPWORDS]
    for t in title_tokens:
        if re.search(rf"\b{re.escape(t)}s?\b", low):
            score += 2.5

    body_low = section["body"].lower()
    for t in _content_tokens(message):
        if t in body_low:
            score += 0.5

    return score


def search_rules(message: str, *, top_k: int = 3, min_score: float = 4.0) -> list[dict[str, Any]]:
    """Return sections most relevant to the query, sorted by score desc."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for s in RULES_HANDBOOK:
        sc = score_section(message, s)
        if sc >= min_score:
            scored.append((sc, s))
    scored.sort(key=lambda x: (-x[0], x[1]["number"]))
    return [s for _, s in scored[:top_k]]


def answer_rules_query(message: str) -> dict[str, Any]:
    """
    Resolve a user message about rules into a ready-to-render answer.

    Returns:
        {
          "mode": "full" | "section" | "toc",
          "text": "<markdown answer>",
          "matched": [section_numbers...]
        }
    """
    if wants_full_handbook(message):
        return {
            "mode": "full",
            "text": render_full_handbook(),
            "matched": [s["number"] for s in RULES_HANDBOOK],
        }

    hits = search_rules(message)
    if hits:
        chunks = [render_section(s) for s in hits]
        header = (
            f"Here is what the **{HANDBOOK_TITLE}** says about your question:\n\n"
            if len(hits) > 1
            else ""
        )
        return {
            "mode": "section",
            "text": header + "\n\n---\n\n".join(chunks),
            "matched": [s["number"] for s in hits],
        }

    # No confident topical match. The orchestrator will try a friendly LLM
    # fallback first; the full handbook is included as a last-resort payload
    # so we can still respond if the LLM is offline.
    return {
        "mode": "no_match",
        "text": render_full_handbook(),
        "matched": [s["number"] for s in RULES_HANDBOOK],
    }
