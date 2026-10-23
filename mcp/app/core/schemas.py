"""Pydantic schemas for the details Luna Lexia extracts from each document type.

Each schema doubles as the Ollama structured-output format for its type, so
field descriptions are written for the model. Every field is optional: a value
that is not printed on the document must stay null rather than be guessed.
Validators accept the formats Indian financial documents commonly print
(``1,23,456.00``, ``31/03/2025``, ``FY 2024-2025``) and normalize them.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from typing import Annotated, Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field

DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%d-%B-%Y",
    "%d-%b-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y/%m/%d",
)
MONTH_FORMATS = ("%Y-%m", "%m/%Y", "%m-%Y", "%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%b-%y", "%b %y")
MAX_TEXT_LENGTH = 300
# Dates outside this range are misreadings (e.g. "3103-03-20" for 31/03/2025).
PLAUSIBLE_YEARS = range(1900, 2101)
# Summary rows that models sometimes list among line items; sums skip them.
SUMMARY_ROW = re.compile(r"\b(total|gross|net)\b", re.IGNORECASE)
# Field-name suffixes whose values have a fixed official format, checked by review rules.
IDENTIFIER_PATTERNS = {
    "pan": (re.compile(r"[A-Z]{5}\d{4}[A-Z]"), "PAN"),
    "tan": (re.compile(r"[A-Z]{4}\d{5}[A-Z]"), "TAN"),
    "gstin": (re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]"), "GSTIN"),
    "ifsc": (re.compile(r"[A-Z]{4}0[A-Z0-9]{6}"), "IFSC"),
}


def clean_text(value: Any) -> str | None:
    """Collapse whitespace, bound length, and treat blanks as missing."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.casefold() in {"null", "none", "n/a", "na", "-"}:
        return None
    return text[:MAX_TEXT_LENGTH]


def parse_amount(value: Any) -> float | None:
    """Read ``1,23,456.00``, ``Rs. 500``, ``(1,200)``, or ``1,234.00 Dr`` as a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"(?i)\b(rs|inr|usd|cr|dr)\b\.?|[₹$,\s()]", "", text)
    try:
        amount = float(text)
    except ValueError:
        return None
    return -amount if negative else amount


def parse_date(value: Any) -> date | None:
    """Parse common date spellings (day before month, as Indian documents print them).

    An unreadable or implausible date is dropped, not guessed.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value if value.year in PLAUSIBLE_YEARS else None
    text = re.sub(r"\s+", " ", str(value)).strip().rstrip(".")
    for date_format in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format).date()
        except ValueError:
            continue
        return parsed if parsed.year in PLAUSIBLE_YEARS else None
    return None


def parse_month(value: Any) -> str | None:
    """Normalize a pay period such as ``March 2025`` or ``03/2025`` to ``2025-03``."""
    if value is None:
        return None
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}"
    text = re.sub(r"\s+", " ", str(value)).strip()
    for month_format in MONTH_FORMATS:
        try:
            parsed = datetime.strptime(text, month_format)
        except ValueError:
            continue
        return f"{parsed.year:04d}-{parsed.month:02d}"
    parsed_date = parse_date(text)
    return f"{parsed_date.year:04d}-{parsed_date.month:02d}" if parsed_date else None


def parse_year_range(value: Any) -> str | None:
    """Normalize ``FY 2024-2025``, ``2024–25``, or ``2024/25`` to ``2024-25``."""
    if value is None:
        return None
    match = re.search(r"(20\d{2})\s*[-–—/]\s*(\d{2,4})", str(value))
    if not match:
        return None
    start = int(match[1])
    return f"{start}-{(start + 1) % 100:02d}"


def parse_identifier(value: Any) -> str | None:
    """Uppercase an ID such as a PAN, TAN, GSTIN, or IFSC and drop inner spaces."""
    text = clean_text(value)
    return re.sub(r"\s+", "", text).upper() if text else None


def parse_last_digits(value: Any) -> str | None:
    """Keep only the last four digits of an account or card number."""
    text = clean_text(value)
    digits = re.sub(r"\D", "", text or "")
    return digits[-4:] if len(digits) >= 4 else None


def pick_choice(options: tuple[str, ...], value: Any) -> str | None:
    """Match ``"Long Term"`` to ``long`` or ``"Form 26AS"`` to ``form_26as``; else null.

    An unrecognized choice becomes null instead of failing the whole extraction.
    """
    if value is None:
        return None
    text = re.sub(r"[\s-]+", "_", str(value).strip().casefold())
    if text in options:
        return text
    return next((option for option in options if text.startswith(option)), None)


def Choice(*options: str) -> Any:
    """A nullable literal whose input is matched leniently by ``pick_choice``."""
    return Annotated[Literal[options] | None, BeforeValidator(partial(pick_choice, options))]


Text = Annotated[str | None, BeforeValidator(clean_text)]
Identifier = Annotated[str | None, BeforeValidator(parse_identifier)]
LastDigits = Annotated[str | None, BeforeValidator(parse_last_digits)]
Money = Annotated[float | None, BeforeValidator(parse_amount)]
Number = Annotated[float | None, BeforeValidator(parse_amount)]
Day = Annotated[date | None, BeforeValidator(parse_date)]
Month = Annotated[str | None, BeforeValidator(parse_month)]
YearRange = Annotated[str | None, BeforeValidator(parse_year_range)]
Regime = Choice("old", "new")


def financial_year_for(day: date) -> str:
    """Return the Indian financial year (April to March) containing ``day``."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def previous_year_range(year_range: str) -> str:
    """Turn an assessment year into its financial year (``2025-26`` -> ``2024-25``)."""
    start = int(year_range[:4]) - 1
    return f"{start}-{(start + 1) % 100:02d}"


def close(actual: float, expected: float) -> bool:
    """Whether two printed amounts agree within rounding (2 units, or 0.01% of large sums)."""
    return abs(actual - expected) <= max(2.0, abs(expected) * 0.0001)


def amount_total(items: list[Any], field: str) -> float | None:
    """Sum ``field`` over nested rows, skipping total rows; None when no row has a value."""
    values = [
        getattr(item, field)
        for item in items
        if getattr(item, field) is not None
        and not SUMMARY_ROW.search(str(getattr(item, "label", None) or getattr(item, "description", None) or ""))
    ]
    return sum(values) if values else None


def money(value: float) -> str:
    return f"{value:,.2f}"


def full_number(name: str, description: str) -> Any:
    """A last-four-digits field that the model fills with the full printed number.

    Models shorten numbers unreliably, so the schema shown to the model asks
    for ``name`` as printed and the validator keeps its last four digits; the
    stored field keeps its ``*_last4`` name.
    """
    return Field(
        None,
        validation_alias=AliasChoices(name, f"{name.removesuffix('_number')}_last4", f"{name}_last4"),
        description=f"{description}, copied in full as printed; only its last four digits are kept",
    )


def field_label(name: str) -> str:
    return name.replace("_", " ").capitalize()


@dataclass(frozen=True)
class KeyFacts:
    """Facts derived from any extraction, stored as indexed columns for filtering."""

    financial_year: str | None = None
    document_date: date | None = None
    issuer: str | None = None
    amount: float | None = None


class Item(BaseModel):
    """A nested row inside an extraction; unknown keys from the model are dropped."""

    model_config = ConfigDict(extra="ignore")


class LineItem(Item):
    label: Text = Field(None, description="The line's printed name")
    amount: Money = None


class LabeledDate(Item):
    label: Text = None
    date: Day = None


class ExtractedDetails(BaseModel):
    """Base class: names the fields that yield the indexed key facts, and checks rules.

    ``financial_year`` comes from the document's own financial year, else its
    assessment year minus one, else the reference date's April-March year.
    ``review_flags`` applies business rules a correct extraction satisfies; a
    failure does not reject the data but marks it for a human to review.
    """

    model_config = ConfigDict(extra="ignore")

    ISSUER_FIELD: ClassVar[str | None] = None
    DATE_FIELDS: ClassVar[tuple[str, ...]] = ()
    AMOUNT_FIELD: ClassVar[str | None] = None

    def reference_date(self) -> date | None:
        return next(
            (getattr(self, name) for name in self.DATE_FIELDS if getattr(self, name, None)), None
        )

    def key_facts(self) -> KeyFacts:
        reference = self.reference_date()
        financial_year = getattr(self, "financial_year", None)
        assessment_year = getattr(self, "assessment_year", None)
        if not financial_year and assessment_year:
            financial_year = previous_year_range(assessment_year)
        if not financial_year and reference:
            financial_year = financial_year_for(reference)
        return KeyFacts(
            financial_year=financial_year,
            document_date=reference,
            issuer=getattr(self, self.ISSUER_FIELD) if self.ISSUER_FIELD else None,
            amount=getattr(self, self.AMOUNT_FIELD) if self.AMOUNT_FIELD else None,
        )

    def review_flags(self) -> list[str]:
        """Return a readable message for every business rule the details break."""
        flags = []
        if self.AMOUNT_FIELD and getattr(self, self.AMOUNT_FIELD) is None:
            flags.append(f"{field_label(self.AMOUNT_FIELD)} was not found.")
        fields = type(self).model_fields
        for name in fields:
            if name.endswith("_from") and f"{name[:-5]}_to" in fields:
                start, end = getattr(self, name), getattr(self, f"{name[:-5]}_to")
                if start and end and start > end:
                    flags.append(f"{field_label(name)} ({start}) is after {name[:-5].replace('_', ' ')} to ({end}).")
            for suffix, (pattern, label) in IDENTIFIER_PATTERNS.items():
                value = getattr(self, name)
                masked = isinstance(value, str) and ("*" in value or "XXXX" in value)
                if (name == suffix or name.endswith(f"_{suffix}")) and value and not masked:
                    if not pattern.fullmatch(value):
                        flags.append(f"{field_label(name)} {value!r} is not a valid {label}.")
        financial_year = getattr(self, "financial_year", None)
        assessment_year = getattr(self, "assessment_year", None)
        if financial_year and assessment_year and previous_year_range(assessment_year) != financial_year:
            flags.append(
                f"Assessment year {assessment_year} does not follow financial year {financial_year}."
            )
        return flags + self.rules()

    def rules(self) -> list[str]:
        """Type-specific business rules; subclasses override."""
        return []


class Payslip(ExtractedDetails):
    ISSUER_FIELD = "employer_name"
    DATE_FIELDS = ("pay_date",)
    AMOUNT_FIELD = "net_pay"

    employer_name: Text = None
    employee_name: Text = None
    employee_id: Text = None
    designation: Text = None
    pan: Identifier = Field(None, description="Employee PAN, as printed (may be masked)")
    uan: Identifier = Field(None, description="Provident fund Universal Account Number")
    pay_period: Month = Field(None, description="Month the salary is for, as YYYY-MM")
    pay_date: Day = None
    paid_days: Number = None
    currency: Identifier = Field(None, description="ISO currency code, e.g. INR")
    earnings: list[LineItem] = Field(
        default_factory=list,
        description="Each earning line, e.g. Basic, HRA, Special Allowance; not the gross total",
    )
    deductions: list[LineItem] = Field(
        default_factory=list,
        description="Each deduction line, e.g. Provident Fund, Income Tax; not the total",
    )
    basic_salary: Money = None
    house_rent_allowance: Money = None
    gross_earnings: Money = None
    total_deductions: Money = None
    net_pay: Money = Field(None, description="Take-home amount credited to the bank")
    income_tax_tds: Money = Field(None, description="Income tax deducted this month")
    employee_provident_fund: Money = None
    professional_tax: Money = None
    bank_name: Text = None
    bank_account_last4: LastDigits = full_number("bank_account_number", "Salary bank account number")

    def reference_date(self) -> date | None:
        if self.pay_date:
            return self.pay_date
        if self.pay_period:
            year, month = (int(part) for part in self.pay_period.split("-"))
            return date(year, month, 1)
        return None

    def rules(self) -> list[str]:
        flags = []
        gross, deductions, net = self.gross_earnings, self.total_deductions, self.net_pay
        if None not in (gross, deductions, net) and not close(gross - deductions, net):
            flags.append(
                f"Gross earnings minus total deductions ({money(gross - deductions)}) "
                f"does not equal net pay ({money(net)})."
            )
        earned = amount_total(self.earnings, "amount")
        if earned is not None and gross is not None and not close(earned, gross):
            flags.append(f"Earning lines add up to {money(earned)}, not gross earnings {money(gross)}.")
        deducted = amount_total(self.deductions, "amount")
        if deducted is not None and deductions is not None and not close(deducted, deductions):
            flags.append(
                f"Deduction lines add up to {money(deducted)}, not total deductions {money(deductions)}."
            )
        if gross is not None and net is not None and net > gross:
            flags.append("Net pay is larger than gross earnings.")
        return flags


class TDSQuarter(Item):
    quarter: Text = Field(None, description="Q1, Q2, Q3, or Q4")
    receipt_number: Text = None
    amount_paid: Money = Field(None, description="Salary paid or credited in the quarter")
    tax_deducted: Money = None
    tax_deposited: Money = None


class SectionDeduction(Item):
    section: Text = Field(None, description="Section, e.g. 80C, 80CCD(1B), 80D, 80G")
    gross_amount: Money = None
    deductible_amount: Money = None


class Form16(ExtractedDetails):
    ISSUER_FIELD = "employer_name"
    DATE_FIELDS = ("period_to", "certificate_date")
    AMOUNT_FIELD = "total_tax_deducted"

    certificate_number: Text = None
    certificate_date: Day = None
    employer_name: Text = None
    employer_tan: Identifier = None
    employer_pan: Identifier = None
    employee_name: Text = None
    employee_pan: Identifier = None
    assessment_year: YearRange = Field(None, description="Assessment year, e.g. 2025-26")
    financial_year: YearRange = Field(None, description="Financial year, e.g. 2024-25")
    period_from: Day = None
    period_to: Day = None
    tax_regime: Regime = Field(None, description="old or new (section 115BAC) tax regime")
    quarterly_tds: list[TDSQuarter] = Field(default_factory=list, description="Part A quarterly summary")
    total_tax_deducted: Money = None
    total_tax_deposited: Money = None
    gross_salary: Money = Field(None, description="Part B gross salary, section 17(1)+17(2)+17(3)")
    salary_section_17_1: Money = None
    perquisites_section_17_2: Money = None
    profits_in_lieu_section_17_3: Money = None
    exemptions_section_10: Money = Field(None, description="Total allowances exempt under section 10")
    hra_exemption: Money = None
    leave_travel_exemption: Money = None
    standard_deduction: Money = None
    professional_tax: Money = None
    income_chargeable_under_salaries: Money = None
    house_property_income_reported: Money = None
    other_income_reported: Money = None
    gross_total_income: Money = None
    chapter_via_deductions: list[SectionDeduction] = Field(default_factory=list)
    total_chapter_via_deductions: Money = None
    total_taxable_income: Money = None
    tax_on_total_income: Money = None
    rebate_87a: Money = None
    surcharge: Money = None
    health_education_cess: Money = None
    relief_section_89: Money = None
    net_tax_payable: Money = None

    def rules(self) -> list[str]:
        flags = []
        if not self.employee_pan:
            flags.append("Employee PAN was not found; it is needed to file the return.")
        quarterly = amount_total(self.quarterly_tds, "tax_deducted")
        total = self.total_tax_deducted
        if quarterly is not None and total is not None and not close(quarterly, total):
            flags.append(f"Quarterly TDS adds up to {money(quarterly)}, not the total {money(total)}.")
        gross, salary_income = self.gross_salary, self.income_chargeable_under_salaries
        if None not in (gross, self.standard_deduction, salary_income):
            expected = (
                gross
                - (self.exemptions_section_10 or 0)
                - self.standard_deduction
                - (self.professional_tax or 0)
            )
            if not close(expected, salary_income):
                flags.append(
                    f"Gross salary less exemptions, standard deduction, and professional tax "
                    f"({money(expected)}) does not equal income under salaries ({money(salary_income)})."
                )
        if None not in (self.tax_on_total_income, self.net_tax_payable):
            expected = (
                self.tax_on_total_income
                - (self.rebate_87a or 0)
                + (self.surcharge or 0)
                + (self.health_education_cess or 0)
                - (self.relief_section_89 or 0)
            )
            if not close(expected, self.net_tax_payable):
                flags.append(
                    f"Tax less rebate plus surcharge and cess, less relief ({money(expected)}), "
                    f"does not equal net tax payable ({money(self.net_tax_payable)})."
                )
        deductions = amount_total(self.chapter_via_deductions, "deductible_amount")
        stated = self.total_chapter_via_deductions
        if deductions is not None and stated is not None and not close(deductions, stated):
            flags.append(
                f"Chapter VI-A deductions add up to {money(deductions)}, not the stated {money(stated)}."
            )
        income, taxable = self.gross_total_income, self.total_taxable_income
        if None not in (income, stated, taxable) and not close(income - stated, taxable):
            flags.append(
                f"Gross total income minus deductions ({money(income - stated)}) does not equal "
                f"total taxable income ({money(taxable)})."
            )
        return flags


class TDSEntry(Item):
    deductor_name: Text = None
    deductor_tan: Identifier = None
    section: Text = Field(None, description="TDS section, e.g. 192, 194A, 194J")
    amount_paid: Money = None
    tax_deducted: Money = None
    tax_deposited: Money = None


class TaxStatement(ExtractedDetails):
    DATE_FIELDS = ("statement_date",)
    AMOUNT_FIELD = "total_tds"

    statement_kind: Choice("form_26as", "ais", "tis") = Field(
        None, description="form_26as, ais (Annual Information Statement), or tis"
    )
    taxpayer_name: Text = None
    pan: Identifier = None
    assessment_year: YearRange = None
    financial_year: YearRange = None
    statement_date: Day = None
    tds_entries: list[TDSEntry] = Field(default_factory=list, max_length=100)
    total_tds: Money = None
    total_tcs: Money = None
    advance_tax_paid: Money = None
    self_assessment_tax_paid: Money = None
    refund_received: Money = None
    salary_reported: Money = None
    interest_income_reported: Money = None
    dividend_income_reported: Money = None
    securities_sale_reported: Money = None

    def rules(self) -> list[str]:
        entries = amount_total(self.tds_entries, "tax_deducted")
        if entries is not None and self.total_tds is not None and not close(entries, self.total_tds):
            return [f"TDS entries add up to {money(entries)}, not the total {money(self.total_tds)}."]
        return []


class ITRAcknowledgement(ExtractedDetails):
    DATE_FIELDS = ("filing_date",)
    AMOUNT_FIELD = "total_income"

    itr_form: Text = Field(None, description="ITR-1, ITR-2, ITR-3, ITR-4...")
    assessment_year: YearRange = None
    taxpayer_name: Text = None
    pan: Identifier = None
    acknowledgement_number: Text = None
    filing_date: Day = None
    filing_section: Text = Field(None, description="e.g. 139(1) original, 139(5) revised")
    tax_regime: Regime = None
    gross_total_income: Money = None
    total_deductions: Money = None
    total_income: Money = None
    net_tax_payable: Money = None
    taxes_paid: Money = None
    refund_due: Money = None
    tax_payable_balance: Money = None

    def rules(self) -> list[str]:
        if (self.refund_due or 0) > 0 and (self.tax_payable_balance or 0) > 0:
            return ["Both a refund and a balance tax payable are shown."]
        return []


class BankTransaction(Item):
    date: Day = None
    description: Text = None
    reference: Text = None
    debit: Money = None
    credit: Money = None
    balance: Money = None


class BankStatement(ExtractedDetails):
    ISSUER_FIELD = "bank_name"
    DATE_FIELDS = ("statement_to", "statement_from")
    AMOUNT_FIELD = "closing_balance"

    bank_name: Text = None
    account_holder: Text = None
    account_number_last4: LastDigits = full_number("account_number", "Account number")
    account_type: Text = Field(None, description="savings, current, salary, NRE...")
    branch: Text = None
    ifsc: Identifier = None
    currency: Identifier = None
    statement_from: Day = None
    statement_to: Day = None
    opening_balance: Money = None
    closing_balance: Money = None
    total_credits: Money = None
    total_debits: Money = None
    interest_credited: Money = None
    transactions: list[BankTransaction] = Field(
        default_factory=list, max_length=100, description="Up to the first 100 transactions"
    )

    def rules(self) -> list[str]:
        flags = []
        values = (self.opening_balance, self.total_credits, self.total_debits, self.closing_balance)
        if None not in values:
            opening, credits, debits, closing = values
            if not close(opening + credits - debits, closing):
                flags.append(
                    f"Opening balance plus credits minus debits ({money(opening + credits - debits)}) "
                    f"does not equal the closing balance ({money(closing)})."
                )
        previous, mismatched = self.opening_balance, 0
        for item in self.transactions:
            if previous is not None and item.balance is not None and (item.debit or item.credit):
                if not close(previous - (item.debit or 0) + (item.credit or 0), item.balance):
                    mismatched += 1
            previous = item.balance
        if mismatched:
            flags.append(
                f"{mismatched} transaction(s) do not match the running balance; "
                "a debit and a credit may be swapped."
            )
        if self.statement_from and self.statement_to:
            outside = [
                item.date
                for item in self.transactions
                if item.date and not self.statement_from <= item.date <= self.statement_to
            ]
            if outside:
                flags.append(f"{len(outside)} transaction(s) fall outside the statement period.")
        return flags


class CardTransaction(Item):
    date: Day = None
    description: Text = None
    amount: Money = None
    kind: Choice("debit", "credit") = None


class CreditCardStatement(ExtractedDetails):
    ISSUER_FIELD = "issuer"
    DATE_FIELDS = ("statement_date", "billing_period_to")
    AMOUNT_FIELD = "total_amount_due"

    issuer: Text = Field(None, description="Card-issuing bank")
    card_name: Text = None
    card_holder: Text = None
    card_last4: LastDigits = full_number("card_number", "Card number (often masked)")
    statement_date: Day = None
    payment_due_date: Day = None
    billing_period_from: Day = None
    billing_period_to: Day = None
    currency: Identifier = None
    total_amount_due: Money = None
    minimum_amount_due: Money = None
    previous_balance: Money = None
    payments_and_credits: Money = None
    purchases_and_debits: Money = None
    finance_charges: Money = None
    credit_limit: Money = None
    available_credit_limit: Money = None
    reward_points_balance: Number = None
    transactions: list[CardTransaction] = Field(
        default_factory=list, max_length=100, description="Up to the first 100 transactions"
    )

    def rules(self) -> list[str]:
        flags = []
        minimum, total = self.minimum_amount_due, self.total_amount_due
        if minimum is not None and total is not None and minimum > total + 1:
            flags.append("Minimum amount due is larger than the total amount due.")
        if self.statement_date and self.payment_due_date and self.payment_due_date < self.statement_date:
            flags.append("Payment due date is before the statement date.")
        return flags


class InvoiceItem(Item):
    description: Text = None
    quantity: Number = None
    unit_price: Money = None
    amount: Money = None


class Invoice(ExtractedDetails):
    ISSUER_FIELD = "vendor_name"
    DATE_FIELDS = ("invoice_date", "billing_period_to")
    AMOUNT_FIELD = "total_amount"

    vendor_name: Text = None
    vendor_gstin: Identifier = None
    invoice_number: Text = None
    invoice_date: Day = None
    due_date: Day = None
    buyer_name: Text = None
    buyer_gstin: Identifier = None
    category: Text = Field(
        None,
        description=(
            "One of: electronics, groceries, food, utilities, telecom, medical, education, "
            "travel, fuel, rent, insurance, subscription, shopping, services, other"
        ),
    )
    account_or_consumer_number: Text = Field(None, description="For utility and telecom bills")
    billing_period_from: Day = None
    billing_period_to: Day = None
    line_items: list[InvoiceItem] = Field(default_factory=list, max_length=100)
    subtotal: Money = None
    discount: Money = None
    cgst: Money = None
    sgst: Money = None
    igst: Money = None
    total_tax: Money = None
    total_amount: Money = Field(None, description="Grand total payable")
    amount_paid: Money = None
    payment_status: Choice("paid", "unpaid", "partially_paid") = None
    payment_method: Text = None
    currency: Identifier = None

    def rules(self) -> list[str]:
        flags = []
        gst = [value for value in (self.cgst, self.sgst, self.igst) if value is not None]
        if gst and self.total_tax is not None and not close(sum(gst), self.total_tax):
            flags.append(f"CGST, SGST, and IGST add up to {money(sum(gst))}, not total tax {money(self.total_tax)}.")
        tax = self.total_tax if self.total_tax is not None else (sum(gst) if gst else None)
        if None not in (self.subtotal, tax, self.total_amount):
            expected = self.subtotal - (self.discount or 0) + tax
            if not close(expected, self.total_amount):
                flags.append(
                    f"Subtotal less discount plus tax ({money(expected)}) does not equal "
                    f"the total ({money(self.total_amount)})."
                )
        items = amount_total(self.line_items, "amount")
        if items is not None and self.subtotal is not None and not close(items, self.subtotal):
            flags.append(f"Line items add up to {money(items)}, not the subtotal {money(self.subtotal)}.")
        return flags


class RentReceipt(ExtractedDetails):
    ISSUER_FIELD = "landlord_name"
    DATE_FIELDS = ("receipt_date", "rent_period_to", "rent_period_from")
    AMOUNT_FIELD = "amount_received"

    landlord_name: Text = None
    landlord_pan: Identifier = None
    tenant_name: Text = None
    property_address: Text = None
    receipt_number: Text = None
    receipt_date: Day = None
    rent_period_from: Day = None
    rent_period_to: Day = None
    monthly_rent: Money = None
    amount_received: Money = None
    payment_mode: Text = None
    financial_year: YearRange = None

    # Rent above this per year needs the landlord's PAN for an HRA claim.
    LANDLORD_PAN_ANNUAL_RENT: ClassVar[float] = 100_000

    def rules(self) -> list[str]:
        if self.monthly_rent and self.monthly_rent * 12 > self.LANDLORD_PAN_ANNUAL_RENT and not self.landlord_pan:
            return ["Annual rent exceeds 1,00,000, so an HRA claim needs the landlord's PAN, which was not found."]
        return []


class InvestmentProof(ExtractedDetails):
    ISSUER_FIELD = "provider_name"
    DATE_FIELDS = ("payment_date", "period_to", "period_from")
    AMOUNT_FIELD = "amount_paid"

    proof_kind: Choice(
        "life_insurance",
        "health_insurance",
        "elss",
        "ppf",
        "nps",
        "epf_vpf",
        "sukanya_samriddhi",
        "nsc",
        "tax_saving_fd",
        "tuition_fees",
        "donation",
        "other",
    ) = None
    tax_section: Text = Field(None, description="Deduction section, e.g. 80C, 80CCD(1B), 80D, 80G")
    provider_name: Text = Field(None, description="Insurer, fund house, bank, school, or charity")
    policy_or_account_number: Text = None
    holder_name: Text = None
    insured_or_beneficiaries: Text = None
    payment_date: Day = None
    period_from: Day = None
    period_to: Day = None
    financial_year: YearRange = None
    amount_paid: Money = None
    eligible_deduction: Money = Field(None, description="Only if printed on the document")
    sum_assured: Money = None
    payment_mode: Text = None


class LoanStatement(ExtractedDetails):
    ISSUER_FIELD = "lender_name"
    DATE_FIELDS = ("certificate_date", "period_to")
    AMOUNT_FIELD = "interest_paid"

    lender_name: Text = None
    borrower_names: Text = None
    loan_account_number: Text = None
    loan_type: Choice("home", "education", "vehicle", "personal", "gold", "other") = None
    property_address: Text = Field(None, description="Home loans only")
    certificate_date: Day = None
    period_from: Day = None
    period_to: Day = None
    financial_year: YearRange = None
    sanctioned_amount: Money = None
    disbursed_amount: Money = None
    interest_rate_percent: Number = None
    emi_amount: Money = None
    principal_repaid: Money = Field(None, description="Principal repaid in the period")
    interest_paid: Money = Field(None, description="Interest paid in the period")
    pre_emi_interest: Money = None
    outstanding_principal: Money = None


class CapitalGainEntry(Item):
    security_name: Text = None
    isin: Identifier = None
    asset_class: Choice("equity_mutual_fund", "debt_mutual_fund", "equity", "other") = None
    term: Choice("short", "long") = None
    units: Number = None
    purchase_date: Day = None
    sale_date: Day = None
    purchase_value: Money = None
    sale_value: Money = None
    gain: Money = Field(None, description="Negative for a loss")


class CapitalGainsStatement(ExtractedDetails):
    ISSUER_FIELD = "provider_name"
    DATE_FIELDS = ("statement_to",)
    AMOUNT_FIELD = "total_gain"

    provider_name: Text = Field(None, description="Broker, AMC, CAMS, or KFintech")
    investor_name: Text = None
    pan: Identifier = None
    financial_year: YearRange = None
    statement_from: Day = None
    statement_to: Day = None
    total_sale_value: Money = None
    short_term_gain: Money = None
    long_term_gain: Money = None
    total_gain: Money = None
    entries: list[CapitalGainEntry] = Field(default_factory=list, max_length=100)

    def rules(self) -> list[str]:
        short, long, total = self.short_term_gain, self.long_term_gain, self.total_gain
        if None not in (short, long, total) and not close(short + long, total):
            return [f"Short- and long-term gains add up to {money(short + long)}, not the total {money(total)}."]
        return []


class IdentityDocument(ExtractedDetails):
    ISSUER_FIELD = "issuing_authority"
    DATE_FIELDS = ("issue_date",)

    id_kind: Choice("pan", "aadhaar", "passport", "driving_licence", "voter_id", "other") = None
    holder_name: Text = None
    id_number: Identifier = None
    date_of_birth: Day = None
    guardian_name: Text = Field(None, description="Father's or guardian's name, if printed")
    address: Text = None
    issue_date: Day = None
    expiry_date: Day = None
    issuing_authority: Text = None

    def rules(self) -> list[str]:
        if self.expiry_date and self.expiry_date < date.today():
            return [f"This document expired on {self.expiry_date}."]
        return []


class OtherDocument(ExtractedDetails):
    ISSUER_FIELD = "issuer"
    DATE_FIELDS = ("document_date",)

    issuer: Text = None
    recipient: Text = None
    document_date: Day = None
    reference_number: Text = None
    financial_year: YearRange = None
    key_amounts: list[LineItem] = Field(default_factory=list, max_length=30)
    key_dates: list[LabeledDate] = Field(default_factory=list, max_length=30)


@dataclass(frozen=True)
class DocumentType:
    """One recognizable kind of document and the schema its details follow."""

    key: str
    label: str
    tag: str | None
    description: str
    schema: type[ExtractedDetails]

    def describe(self) -> dict[str, Any]:
        """A JSON-ready description for clients deciding which fields to read."""
        return {
            "doc_type": self.key,
            "label": self.label,
            "description": self.description,
            "fields": self.schema.model_json_schema(),
        }


DOCUMENT_TYPES: dict[str, DocumentType] = {
    document_type.key: document_type
    for document_type in (
        DocumentType(
            "payslip",
            "Payslip",
            "payslip",
            "Monthly salary slip or pay statement issued by an employer.",
            Payslip,
        ),
        DocumentType(
            "form_16",
            "Form 16",
            "form 16",
            "Form 16 TDS certificate for salary (Part A and/or Part B) issued by an employer.",
            Form16,
        ),
        DocumentType(
            "tax_statement",
            "Tax statement",
            "tax statement",
            "Form 26AS, Annual Information Statement (AIS), or Taxpayer Information Summary (TIS).",
            TaxStatement,
        ),
        DocumentType(
            "itr_acknowledgement",
            "ITR acknowledgement",
            "itr",
            "Income tax return acknowledgement (ITR-V) or a filed return summary.",
            ITRAcknowledgement,
        ),
        DocumentType(
            "bank_statement",
            "Bank statement",
            "bank statement",
            "Savings or current account statement or passbook listing transactions.",
            BankStatement,
        ),
        DocumentType(
            "credit_card_statement",
            "Credit card statement",
            "credit card statement",
            "Monthly credit card bill with amount due and card transactions.",
            CreditCardStatement,
        ),
        DocumentType(
            "invoice",
            "Invoice or bill",
            "invoice",
            "Purchase invoice, receipt, utility or telecom bill, or other bill of sale.",
            Invoice,
        ),
        DocumentType(
            "rent_receipt",
            "Rent receipt",
            "rent receipt",
            "Receipt or agreement showing house rent paid to a landlord (HRA proof).",
            RentReceipt,
        ),
        DocumentType(
            "investment_proof",
            "Investment or deduction proof",
            "tax proof",
            "Proof for a tax deduction: insurance premium receipt, ELSS, PPF, NPS, "
            "tuition fees, donation receipt, and similar.",
            InvestmentProof,
        ),
        DocumentType(
            "loan_statement",
            "Loan statement",
            "loan",
            "Loan account statement or interest certificate (home, education, vehicle, personal).",
            LoanStatement,
        ),
        DocumentType(
            "capital_gains_statement",
            "Capital gains statement",
            "capital gains",
            "Capital gains or tax P&L statement from a broker, AMC, CAMS, or KFintech.",
            CapitalGainsStatement,
        ),
        DocumentType(
            "identity_document",
            "Identity document",
            "identity",
            "PAN card, Aadhaar, passport, driving licence, or voter ID.",
            IdentityDocument,
        ),
        DocumentType(
            "other",
            "Other document",
            None,
            "Any other document, such as a letter, contract, or certificate.",
            OtherDocument,
        ),
    )
}

DocTypeName = Literal[tuple(DOCUMENT_TYPES)]  # type: ignore[valid-type]

# Bump when a schema changes so backfill re-extracts documents stored under an older one.
SCHEMA_VERSION = 1


def document_type(key: str) -> DocumentType:
    """Return the registered type for ``key`` or explain the valid choices."""
    try:
        return DOCUMENT_TYPES[key]
    except KeyError:
        raise ValueError(
            f"Unknown doc_type {key!r}; use one of: {', '.join(DOCUMENT_TYPES)}."
        ) from None
