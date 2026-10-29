import csv
import hashlib
import io
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

from ofxparse import OfxParser
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.account import Account
from app.models.category import Category
from app.models.rule import Rule
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionImport
from app.services import recurring_match_service
from app.services.credit_card_service import apply_effective_date
from app.services.rule_engine import apply_rule_actions, evaluate_conditions, merge_notes
from app.services.rule_service import apply_rules_to_transaction, preview_rules_for_transaction
from app.services.fx_rate_service import stamp_primary_amount
from app.services.payee_service import get_or_create_payee


# Descriptions used by some Brazilian banks for balance-summary rows that
# arrive looking like transactions but are not: Banco do Brasil emits them as
# <STMTTRN> blocks, BS2 as rows of its statement CSV. Matched
# case-insensitively against the row's description fields.
_BALANCE_ROW_DESCRIPTIONS = (
    "saldo anterior",
    "saldo inicial",
    "saldo do dia",
    "saldo final",
    "s a l d o",
)


def _decode_statement_bytes(content: bytes) -> tuple[str, str]:
    """Decode statement content to text, trying UTF-8 then falling back to Latin-1.

    Returns (text, encoding) so callers can re-encode consistently later.
    """
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return content.decode("latin-1"), "latin-1"


def _patch_empty_fitids(text: str) -> str:
    """Synthesize a FITID for STMTTRN blocks that have an empty/missing one.

    Banco do Brasil (and a few other Brazilian banks) emit balance-summary
    rows as <STMTTRN> blocks with empty <FITID> tags, which makes ofxparse
    abort the entire import with "Empty FIT id (a required field)". We patch
    each affected block with a deterministic synthetic FITID so parsing
    succeeds; balance rows are filtered out later by description.
    """
    def _replace(match: re.Match) -> str:
        block = match.group(0)
        fitid_match = re.search(r"<FITID>([^<\r\n]*)", block, re.IGNORECASE)
        has_value = fitid_match and fitid_match.group(1).strip()
        if has_value:
            return block

        seed = hashlib.sha1(block.encode("utf-8", errors="replace")).hexdigest()[:16].upper()
        synthetic = f"SYNTH-{seed}"
        if fitid_match:
            return block[: fitid_match.start(1)] + synthetic + block[fitid_match.end(1):]
        # No FITID tag at all — inject one right after the opening <STMTTRN>
        return re.sub(
            r"(<STMTTRN>)",
            rf"\1\n<FITID>{synthetic}",
            block,
            count=1,
            flags=re.IGNORECASE,
        )

    return re.sub(
        r"<STMTTRN>.*?</STMTTRN>",
        _replace,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _ensure_ofx_sgml_header(text: str, encoding: str) -> str:
    """Prepend a legacy OFX 1.x SGML header for OFX 2.x files that omit it.

    OFX 2.x is plain XML that goes straight into its first tag, with no
    colon-delimited SGML header block (e.g. Erste Bank's "MS Money Sunset
    Deluxe" export). ofxparse only looks for encoding hints in the bytes
    preceding the file's first "<"; when that's empty it silently assumes
    ASCII and crashes on any non-ASCII byte. Prepending a synthetic SGML
    header routes the file through ofxparse's existing, correctly-working
    SGML decode path instead of its broken auto-detection (see
    https://github.com/jseutter/ofxparse/issues/133).

    The trigger is therefore "nothing precedes the first tag", not "starts
    with <?xml": the XML declaration is optional in XML 1.0, so an OFX 2.x
    file may open with just its <?OFX ... ?> instruction, or with <OFX>
    itself, and those hit the same ofxparse bug. Anything else already has a
    legacy header, and a file with no tag at all is left for ofxparse to
    reject on its own terms.

    `encoding` must match whatever the caller will re-encode `text` with,
    so the declared header and the actual bytes stay consistent.
    """
    preamble, first_tag, _ = text.lstrip("\ufeff \t\r\n").partition("<")
    if not first_tag or preamble.strip():
        return text
    if encoding == "latin-1":
        enc_lines = "ENCODING:USASCII\r\nCHARSET:8859-1\r\n"
    else:
        enc_lines = "ENCODING:UTF-8\r\nCHARSET:NONE\r\n"
    header = (
        f"OFXHEADER:100\r\nDATA:OFXSGML\r\nVERSION:102\r\nSECURITY:NONE\r\n"
        f"{enc_lines}COMPRESSION:NONE\r\nOLDFILEUID:NONE\r\nNEWFILEUID:NONE\r\n\r\n"
    )
    return header + text


def _preprocess_ofx(content: bytes) -> bytes:
    """Apply text-level fixups ofxparse needs before it can parse the file."""
    text, encoding = _decode_statement_bytes(content)
    text = _patch_empty_fitids(text)
    text = _ensure_ofx_sgml_header(text, encoding)
    return text.encode(encoding, errors="replace")


def _is_balance_summary_row(description: str | None) -> bool:
    if not description:
        return False
    normalized = description.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _BALANCE_ROW_DESCRIPTIONS)


def parse_ofx(content: bytes) -> list[TransactionImport]:
    """Parse OFX file content and return transactions."""
    content = _preprocess_ofx(content)
    ofx = OfxParser.parse(io.BytesIO(content))
    transactions = []

    for account in ofx.accounts:
        for txn in account.statement.transactions:
            raw_payee = getattr(txn, 'payee', None) or None
            description = txn.memo or txn.payee or "Unknown"
            if _is_balance_summary_row(description):
                continue
            external_id = getattr(txn, 'id', None)
            # Synthetic IDs are added only to make ofxparse happy; do not
            # persist them as external_id since they are not stable bank
            # identifiers.
            if external_id and external_id.startswith("SYNTH-"):
                external_id = None
            transactions.append(TransactionImport(
                description=description,
                amount=abs(Decimal(str(txn.amount))),
                date=txn.date.date() if hasattr(txn.date, 'date') else txn.date,
                type="credit" if txn.amount > 0 else "debit",
                external_id=external_id,
                payee_raw=raw_payee,
            ))

    return transactions


# QIF "D" lines carry no format metadata; US-first order is the historical
# default. The Quicken apostrophe variants are always month-first.
_QIF_FALLBACK_DATE_FORMATS = [
    '%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d',
    "%m/%d'%Y", "%m/%d'%y",
    '%m/%d/%y', '%d/%m/%y',
]


def _qif_date_formats(date_format: str | None, raw_dates: list[str]) -> list[str]:
    """Decide the strptime formats for a QIF file's dates, once per file.

    An explicit user choice is strict (like parse_csv): only that format and
    its 2-digit-year variant are accepted. Otherwise the order is inferred
    from the whole file — a first component > 12 can only be a day, so the
    file is DD/MM; per-line first-match parsing would silently mix MM/DD and
    DD/MM within a single import for the ambiguous days 1-12.
    """
    if date_format and date_format in DATE_FORMAT_MAP:
        fmt = DATE_FORMAT_MAP[date_format]
        return [fmt, fmt.replace('%Y', '%y')]

    saw_day_first = saw_month_first = False
    for value in raw_dates:
        match = re.match(r"^(\d{1,2})/(\d{1,2})/\d{2,4}$", value)
        if not match:
            continue
        first, second = int(match.group(1)), int(match.group(2))
        if first > 12 >= second:
            saw_day_first = True
        if second > 12 >= first:
            saw_month_first = True

    if saw_day_first and not saw_month_first:
        return ['%d/%m/%Y', '%d/%m/%y'] + _QIF_FALLBACK_DATE_FORMATS
    return list(_QIF_FALLBACK_DATE_FORMATS)


def parse_qif(content: bytes, date_format: str | None = None) -> list[TransactionImport]:
    """Parse QIF file content and return transactions.

    date_format: optional explicit format (see DATE_FORMAT_MAP keys); when
    omitted, the day/month order is inferred from the whole file.
    """
    # Try UTF-8 first, fall back to Latin-1 for legacy software (e.g. Microsoft Money)
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = content.decode('latin-1')
    transactions = []

    # Split into transaction blocks by "^"
    blocks = text.split('^')

    raw_dates = [
        stripped[1:].strip()
        for block in blocks
        for stripped in (line.strip() for line in block.strip().splitlines())
        if stripped.startswith('D')
    ]
    date_formats = _qif_date_formats(date_format, raw_dates)

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        txn_date = None
        amount = None
        payee = None
        memo = None

        for line in lines:
            line = line.strip()
            if not line:
                continue
            tag, value = line[0], line[1:]
            if tag == 'D':
                for fmt in date_formats:
                    try:
                        txn_date = datetime.strptime(value.strip(), fmt).date()
                        break
                    except ValueError:
                        continue
            elif tag == 'T' or tag == 'U':
                try:
                    amount = Decimal(value.strip().replace(',', ''))
                except Exception:
                    pass
            elif tag == 'P':
                payee = value.strip()
            elif tag == 'M':
                memo = value.strip()

        if txn_date is None or amount is None:
            continue

        description = payee or memo or "Unknown"
        transactions.append(TransactionImport(
            description=description,
            amount=abs(amount),
            date=txn_date,
            type="credit" if amount > 0 else "debit",
            payee_raw=payee,
        ))

    return transactions


def parse_camt(content: bytes) -> list[TransactionImport]:
    """Parse CAMT.052/CAMT.053 (ISO 20022) XML file content and return transactions."""
    root = ET.fromstring(content)

    # Detect namespace dynamically
    ns_match = re.match(r'\{(.+?)\}', root.tag)
    ns = ns_match.group(1) if ns_match else ''
    nsmap = {'ns': ns} if ns else {}

    def find(element, path):
        """Find element with or without namespace."""
        if nsmap:
            parts = path.split('/')
            ns_path = '/'.join(f'ns:{p}' for p in parts)
            return element.find(ns_path, nsmap)
        return element.find(path)

    def findall(element, path):
        if nsmap:
            parts = path.split('/')
            ns_path = '/'.join(f'ns:{p}' for p in parts)
            return element.findall(ns_path, nsmap)
        return element.findall(path)

    def find_text(element, path):
        el = find(element, path)
        return el.text if el is not None else None

    transactions = []

    # Navigate: Document > BkToCstmrStmt > Stmt > Ntry (CAMT.053, end-of-day statement)
    # Fallback: Document > BkToCstmrAcctRpt > Rpt > Ntry (CAMT.052, intraday report —
    # same Ntry sub-schema, different root/container element). Several European banks,
    # including German Volksbanken/Raiffeisenbanken, only offer CAMT.052 exports.
    stmts = findall(root, 'BkToCstmrStmt/Stmt') or findall(root, 'BkToCstmrAcctRpt/Rpt')
    for stmt in stmts:
        for ntry in findall(stmt, 'Ntry'):
            # Entry status: BOOK (final) vs. PDNG/INFO (pending/informational).
            # CAMT.052 intraday reports commonly include PDNG entries for
            # transactions that haven't settled yet; the same transaction is
            # reported again as BOOK once it settles. Skip anything that
            # isn't BOOK to avoid importing it twice. Status is either a
            # plain code (<Sts>BOOK</Sts>) or wrapped (<Sts><Cd>BOOK</Cd></Sts>)
            # depending on the schema version. Check the wrapped form first:
            # in pretty-printed XML the <Sts> element's own text is the
            # whitespace before <Cd>, which would otherwise mask the real code.
            status = find_text(ntry, 'Sts/Cd') or find_text(ntry, 'Sts')
            if status and status.strip().upper() != 'BOOK':
                continue

            # Amount
            amt_el = find(ntry, 'Amt')
            if amt_el is None:
                continue
            try:
                amount = Decimal(amt_el.text)
            except Exception:
                continue

            # Credit/Debit indicator
            cdt_dbt = find_text(ntry, 'CdtDbtInd')
            txn_type = "credit" if cdt_dbt == "CRDT" else "debit"

            # Date: try BookgDt/Dt then ValDt/Dt
            date_str = find_text(ntry, 'BookgDt/Dt') or find_text(ntry, 'ValDt/Dt')
            if not date_str:
                continue
            try:
                txn_date = datetime.strptime(date_str.strip(), '%Y-%m-%d').date()
            except ValueError:
                continue

            # Description from various paths
            description = (
                find_text(ntry, 'NtryDtls/TxDtls/RmtInf/Ustrd')
                or find_text(ntry, 'NtryDtls/TxDtls/RltdPties/Cdtr/Nm')
                or find_text(ntry, 'NtryDtls/TxDtls/RltdPties/Dbtr/Nm')
                or find_text(ntry, 'AddtlNtryInf')
                or "Unknown"
            )

            # Extract currency from Ccy attribute on Amt element
            txn_currency = amt_el.get('Ccy') or None

            transactions.append(TransactionImport(
                description=description,
                amount=abs(amount),
                date=txn_date,
                type=txn_type,
                currency=txn_currency,
            ))

    return transactions


DATE_FORMAT_MAP = {
    'DD/MM/YYYY': '%d/%m/%Y',
    'MM/DD/YYYY': '%m/%d/%Y',
    'YYYY-MM-DD': '%Y-%m-%d',
}

# Securo fields a CSV column can be mapped to. Used to validate the
# user-supplied column_mapping and to drive the import-UI dropdowns.
CSV_MAPPABLE_FIELDS = (
    'date', 'description', 'amount', 'type',
    'category', 'currency', 'fx_rate', 'inflow', 'outflow',
    'payee', 'external_id', 'notes',
)


def _sniff_csv_dialect(text: str):
    """Detect the CSV dialect (delimiter/quoting), falling back to comma."""
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
    except csv.Error:
        return csv.excel


def detect_csv_columns(content: bytes) -> list[str]:
    """Return the CSV header column names exactly as they appear in the file.

    Used by the import preview so the UI can offer accurate column-mapping
    dropdowns instead of guessing headers client-side.
    """
    text = content.decode('utf-8-sig')  # Handle BOM
    dialect = _sniff_csv_dialect(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [f.strip() for f in (reader.fieldnames or []) if f and f.strip()]


# Noh (noh.com.br) is a Brazilian joint account for couples. Each partner has
# their own pocket, and when one of them spends, Noh mirrors the charge with a
# "Saldo movido" row that moves the same amount between pockets. The export
# carries only one leg of that internal move, always as an outflow, so every
# such row re-books spending the file already lists: 190 of the 481 rows, and
# R$ 51.600, in a three-month export. Each one was checked against the charges
# of its own day — it equals either a single charge exactly, or a charge netted
# against a card adjustment that posted days later — so none of them is money
# leaving the account, and none says anything the charge rows don't.
#
# "Saldo adicionado" is the opposite and is kept: that is money put into the
# account from outside it.
_NOH_HEADER = ('data', 'iniciador', 'método', 'descrição', 'valor', 'tipo')
_NOH_POCKET_TRANSFER = 'saldo movido'


def is_noh_csv(content: bytes) -> bool:
    """True when the upload is a Noh statement export.

    Recognised by its exact column layout: Noh offers no Open Finance feed and
    no per-row identifier, so the header is all there is to go on.
    """
    try:
        header = detect_csv_columns(content)
    except Exception:
        return False
    return tuple(column.lower() for column in header) == _NOH_HEADER


def parse_noh_csv(content: bytes) -> list[TransactionImport]:
    """Parse a Noh statement export.

    Fixed layout, so nothing here is detected: DD/MM/YYYY dates, "-R$ 1.234,56"
    amounts whose sign (not the Tipo column) decides debit vs credit, and the
    Iniciador/Método pair kept as notes — on a couple's account "who spent it"
    is the thing the description never says.

    Two kinds of row come back pre-excluded rather than dropped, so the import
    review shows what was left out and the user can put any of it back: the
    "Saldo movido" pocket transfers, and same-day reversal pairs.

    Every row gets a deterministic external_id. Noh issues no transaction id,
    and the field-based duplicate check this would otherwise fall back to also
    collapses rows that are identical *and* real — one statement day holds
    one vendor billed twice at R$ 13,00, and another two boletos of R$ 911,24 —
    so re-importing an overlapping date range has to match on something that
    survives a legitimate repeat. The occurrence counter is per identical row
    within a day, which keeps an id stable no matter where the export window
    starts or how Noh orders a day's rows.
    """
    text = content.decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(text), dialect=_sniff_csv_dialect(text))

    parsed = []
    for raw_row in reader:
        row = {
            (k or '').lower().strip(): (v or '').strip()
            for k, v in raw_row.items()
        }
        try:
            txn_date = datetime.strptime(row['data'], '%d/%m/%Y').date()
            signed = Decimal(normalize_amount(row['valor']))
        except Exception:
            continue  # Skip unreadable rows, as the generic CSV parser does
        parsed.append((txn_date, signed, row))

    # A charge and its equal, opposite twin on the same day under the same
    # description are a card reversal, not income — Noh posts both legs. They
    # net to zero, so both are excluded; keeping them would book the positive
    # leg as R$ 361,20 of income from a supermarket.
    by_day_description: dict[tuple, list[int]] = {}
    for index, (txn_date, _signed, row) in enumerate(parsed):
        by_day_description.setdefault(
            (txn_date, row.get('descrição', '')), []
        ).append(index)

    reversals: set[int] = set()
    for group in by_day_description.values():
        for index in group:
            if index in reversals or parsed[index][1] <= 0:
                continue
            twin = next(
                (
                    other for other in group
                    if other not in reversals
                    and parsed[other][1] == -parsed[index][1]
                ),
                None,
            )
            if twin is not None:
                reversals.update((index, twin))

    occurrences: dict[str, int] = {}
    transactions = []
    for index, (txn_date, signed, row) in enumerate(parsed):
        initiator = row.get('iniciador', '')
        method = row.get('método', '')
        description = row.get('descrição', '') or 'Noh'

        fingerprint = '|'.join(
            [txn_date.isoformat(), initiator, method, description, str(signed)]
        )
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        digest = hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:8]

        is_pocket_transfer = description.lower() == _NOH_POCKET_TRANSFER
        is_reversal = index in reversals
        note_parts = [initiator, method]
        if is_pocket_transfer:
            note_parts.append('transferência entre caixinhas')
        elif is_reversal:
            note_parts.append('estorno (par que se anula no mesmo dia)')

        transactions.append(TransactionImport(
            description=description,
            amount=abs(signed),
            date=txn_date,
            type='credit' if signed > 0 else 'debit',
            external_id=f'noh:{txn_date.isoformat()}:{signed}:{digest}:{occurrence}',
            notes=' · '.join(part for part in note_parts if part),
            excluded=is_pocket_transfer or is_reversal,
        ))

    return transactions


# C6 Bank prints eight lines of heading above the table.
_C6_HEADER = (
    'data lançamento', 'data contábil', 'título', 'descrição',
    'entrada(r$)', 'saída(r$)', 'saldo do dia(r$)',
)


def _statement_body_from_header(
    content: bytes, header: tuple[str, ...], delimiter: str = ','
) -> str | None:
    """Return a statement CSV's body, starting at the row matching `header`.

    Several banks print the account and period above the table, so the upload
    is only a CSV from its header row down and csv.DictReader on the raw bytes
    reads the first heading line as the header. The row is found by its column
    names rather than by counting heading lines, so a heading that gains or
    loses a line still imports. Returns None when the file carries no such
    header, which is how each bank's `is_*_csv` says "not mine".

    The BOM is stripped per line, not just at the front: BS2 writes one before
    arbitrary rows in the middle of the file, which would otherwise end up
    glued to that row's date.
    """
    try:
        text, _ = _decode_statement_bytes(content)
    except Exception:
        return None
    lines = [line.lstrip('\ufeff') for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line.lower().startswith(header[0]):
            continue
        found = tuple(
            column.strip().lower()
            for column in next(csv.reader([line], delimiter=delimiter))
        )
        return '\n'.join(lines[index:]) if found == header else None
    return None


def is_c6_csv(content: bytes) -> bool:
    """True when the upload is a C6 Bank current-account statement export."""
    return _statement_body_from_header(content, _C6_HEADER) is not None


def parse_c6_csv(content: bytes) -> list[TransactionImport]:
    """Parse a C6 Bank current-account statement export.

    Fixed layout: DD/MM/YYYY dates, split Entrada/Saída columns with 0.00 in
    the unused one, and dot-decimal amounts (not the comma-decimal the rest of
    the Brazilian exports here use — `normalize_amount` reads both).

    `Título` carries the counterparty and becomes the description; `Descrição`
    repeats it on half the rows and otherwise holds the invoice reference
    ("NF-10") or a generic "TRANSF ENVIADA PIX", so it is kept as a note only
    where it says something new. `Data Contábil` is noted when it differs from
    the posting date, which happens when a transfer settles a day or two later.

    `Saldo do Dia` is the running end-of-day balance, not a transaction, so it
    is not imported — but it closes exactly against the summed rows, which is
    what the parser's test checks.

    Like Noh, C6 issues no transaction id, so each row gets a deterministic
    one; see `parse_noh_csv` for why the field-based fallback is not enough.
    The fingerprint is case-folded here because one export already writes the
    same counterparty both in upper case and in title case — an id that moved
    with the casing would re-import rows that are already in.
    """
    body = _statement_body_from_header(content, _C6_HEADER)
    if body is None:
        raise ValueError("Not a C6 Bank statement export")

    occurrences: dict[str, int] = {}
    transactions = []
    for raw_row in csv.DictReader(io.StringIO(body)):
        row = {
            (k or '').strip().lower(): (v or '').strip()
            for k, v in raw_row.items()
        }
        try:
            posted = row['data lançamento']
            txn_date = datetime.strptime(posted, '%d/%m/%Y').date()
            inflow = Decimal(normalize_amount(row['entrada(r$)']) or '0')
            outflow = Decimal(normalize_amount(row['saída(r$)']) or '0')
        except Exception:
            continue  # Heading leftovers and the trailing blank line land here
        signed = inflow - outflow
        if not signed:
            continue  # No money moved; C6 writes 0.00 in the column it is not using

        title = row.get('título', '') or 'C6'
        memo = row.get('descrição', '')
        booked = row.get('data contábil', '')

        note_parts = []
        if memo and memo.casefold() != title.casefold():
            note_parts.append(memo)
        if booked and booked != posted:
            note_parts.append(f'compensado em {booked}')

        fingerprint = '|'.join(
            [txn_date.isoformat(), title.casefold(), memo.casefold(), str(signed)]
        )
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        digest = hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:8]

        transactions.append(TransactionImport(
            description=title,
            amount=abs(signed),
            date=txn_date,
            type='credit' if signed > 0 else 'debit',
            external_id=f'c6:{txn_date.isoformat()}:{signed}:{digest}:{occurrence}',
            notes=' · '.join(note_parts) or None,
        ))

    return transactions


# BS2 prints ten lines of heading above the table and separates with ";".
_BS2_HEADER = (
    'datacontabil', 'data operação', 'tipo', 'detalhe', 'banco', 'agencia',
    'conta', 'identificador da transação', 'identificador da movimentação',
    'valor', 'observação',
)


def is_bs2_csv(content: bytes) -> bool:
    """True when the upload is a Banco BS2 business statement export."""
    return _statement_body_from_header(content, _BS2_HEADER, ';') is not None


def parse_bs2_csv(content: bytes) -> list[TransactionImport]:
    """Parse a Banco BS2 business current-account statement export.

    Semicolon-separated, "R$ -1.024,51" amounts, DD/MM/YYYY in `DataContabil`
    while `Data Operação` repeats it with the time.

    Unlike the other statement exports here, BS2 numbers its rows:
    `Identificador da movimentação` is the account's own ledger key (bank,
    branch, account, date and a daily sequence), unique per row, so duplicate
    detection uses the bank's identifier instead of a synthetic one. Its
    `Identificador da transação` — a PIX end-to-end id, a TED id or a UUID —
    stands in on the rare row that has no movement key.

    `Detalhe` carries the counterparty with its CNPJ or CPF
    ("TED C RECEBIDA - 00000000000191 - CLIENTE DELTA LTDA") and becomes the
    description; `Tipo` and `Observação` are kept as notes.

    The opening and closing balance rows are dropped. They are not optional
    noise: the closing one is a full-arity row whose amount is the balance
    itself, so importing it books the entire account balance as a credit,
    while the opening one is written a field short, which shifts every column
    after it.
    """
    body = _statement_body_from_header(content, _BS2_HEADER, ';')
    if body is None:
        raise ValueError("Not a BS2 statement export")

    transactions = []
    for raw_row in csv.DictReader(io.StringIO(body), delimiter=';'):
        row = {
            (k or '').strip().lower(): (v or '').strip()
            for k, v in raw_row.items() if k is not None
        }
        detail = row.get('detalhe', '')
        kind = row.get('tipo', '')
        if _is_balance_summary_row(kind) or _is_balance_summary_row(detail):
            continue
        try:
            txn_date = datetime.strptime(row['datacontabil'], '%d/%m/%Y').date()
            signed = Decimal(normalize_amount(row['valor']))
        except Exception:
            continue  # Heading leftovers and the closing footer line land here
        if not signed:
            continue

        external_id = (
            row.get('identificador da movimentação')
            or row.get('identificador da transação')
            or None
        )
        notes = ' · '.join(
            part for part in (kind, row.get('observação', ''))
            if part and part.casefold() != detail.casefold()
        )

        transactions.append(TransactionImport(
            description=detail or kind or 'BS2',
            amount=abs(signed),
            date=txn_date,
            type='credit' if signed > 0 else 'debit',
            external_id=external_id,
            notes=notes or None,
        ))

    return transactions


def parse_csv(
    content: bytes,
    date_format: str | None = None,
    flip_amount: bool = False,
    inflow_column: str | None = None,
    outflow_column: str | None = None,
    column_mapping: dict[str, str] | None = None,
) -> list[TransactionImport]:
    """Parse CSV file content and return transactions.

    Attempts to detect common column formats:
    - date, description, amount
    - data, descricao, valor (Portuguese)

    Options:
    - date_format: explicit date format (DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD)
    - flip_amount: negate all parsed amounts
    - inflow_column/outflow_column: use split columns instead of single amount
    - column_mapping: explicit Securo-field -> CSV-header map. Any field
      present here overrides auto-detection; unmapped fields still auto-detect.
    """
    text = content.decode('utf-8-sig')  # Handle BOM
    dialect = _sniff_csv_dialect(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)

    # Normalize field names
    fieldnames = [f.lower().strip() if f is not None else "" for f in (reader.fieldnames or [])]

    # Map common column names
    date_cols = ['date', 'data', 'dt', 'transaction_date', 'data_transacao']
    desc_cols = ['description', 'descricao', 'desc', 'memo', 'historico', 'lancamento']
    amount_cols = ['amount', 'valor', 'value', 'quantia']
    type_cols = ['type', 'tipo']
    category_cols = ['category', 'categoria']
    currency_cols = ['currency', 'moeda', 'currency_code']
    fx_rate_cols = ['fx_rate', 'fx_rate_used', 'taxa_cambio', 'exchange_rate', 'taxa']
    payee_cols = ['payee', 'merchant', 'beneficiary', 'beneficiario', 'pagador']
    external_id_cols = [] # External ID must be mapped explicitly
    notes_cols = ['notes', 'nota', 'observacao']

    # Normalize the user-supplied column mapping (Securo field -> CSV header).
    mapping = {
        field: value.lower().strip()
        for field, value in (column_mapping or {}).items()
        if field in CSV_MAPPABLE_FIELDS and value and value.strip()
    }

    def find_col(candidates):
        for c in candidates:
            if c in fieldnames:
                return c
        return None

    def resolve_col(field, candidates):
        """Resolve a CSV column for a Securo field.

        An explicit user mapping always wins; otherwise fall back to
        auto-detection against the known column-name candidates.
        """
        mapped = mapping.get(field)
        if mapped:
            if mapped not in fieldnames:
                raise ValueError(
                    f"Mapped column '{mapped}' for field '{field}' not found in CSV. "
                    f"Available columns: {', '.join(fieldnames)}"
                )
            return mapped
        return find_col(candidates)

    date_col = resolve_col('date', date_cols)
    desc_col = resolve_col('description', desc_cols)

    # In split mode, we don't require a single amount column. The inflow/outflow
    # columns may come from the explicit args or from the column mapping.
    inflow_col = (inflow_column or mapping.get('inflow') or '').lower().strip() or None
    outflow_col = (outflow_column or mapping.get('outflow') or '').lower().strip() or None
    use_split = bool(inflow_col and outflow_col)

    if use_split:
        if inflow_col not in fieldnames or outflow_col not in fieldnames:
            raise ValueError(f"Inflow/outflow columns not found in CSV. Available columns: {', '.join(fieldnames)}")
        amount_col = None
    else:
        amount_col = resolve_col('amount', amount_cols)

    type_col = resolve_col('type', type_cols)
    category_col = resolve_col('category', category_cols)
    currency_col = resolve_col('currency', currency_cols)
    fx_rate_col = resolve_col('fx_rate', fx_rate_cols)
    payee_col = resolve_col('payee', payee_cols)
    external_id_col = resolve_col('external_id', external_id_cols)
    notes_col = resolve_col('notes', notes_cols)

    if not date_col or not desc_col:
        raise ValueError(
            f"Could not detect CSV columns. Found: {', '.join(fieldnames)}. "
            f"Expected columns like: date, description, amount (or Portuguese equivalents: data, descricao, valor)"
        )
    if not use_split and not amount_col:
        raise ValueError(
            f"Could not detect amount column. Found: {', '.join(fieldnames)}. "
            f"Expected a column named: {', '.join(amount_cols)}"
        )

    # Determine date formats to try
    if date_format and date_format in DATE_FORMAT_MAP:
        date_formats = [DATE_FORMAT_MAP[date_format]]
    else:
        date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y', '%d.%m.%Y']

    transactions = []
    for row in reader:
        # Normalize row keys
        row = {k.lower().strip() if k is not None else "": v for k, v in row.items()}

        # Parse date
        date_str = row[date_col].strip()
        txn_date = None
        for fmt in date_formats:
            try:
                txn_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

        if not txn_date:
            continue  # Skip invalid dates

        # Parse amount
        if use_split:
            inflow_str = normalize_amount(row.get(inflow_col, ""))
            outflow_str = normalize_amount(row.get(outflow_col, ""))

            try:
                inflow = Decimal(inflow_str) if inflow_str else Decimal('0')
            except Exception:
                inflow = Decimal('0')
            try:
                outflow = Decimal(outflow_str) if outflow_str else Decimal('0')
            except Exception:
                outflow = Decimal('0')

            if inflow > 0:
                amount = inflow
                txn_type = "credit"
            elif outflow > 0:
                amount = outflow
                txn_type = "debit"
            else:
                continue  # Skip rows with no amount
        else:
            amount_str = normalize_amount(row[amount_col])

            try:
                amount = Decimal(amount_str)
            except Exception:
                continue  # Skip invalid amounts

            if flip_amount:
                amount = -amount

            if type_col and row.get(type_col, '').strip() in ('credit', 'debit'):
                txn_type = row[type_col].strip()
            else:
                txn_type = "credit" if amount > 0 else "debit"
            amount = abs(amount)

        # Extract optional category, currency and fx_rate from CSV columns
        category_name = row[category_col].strip() if category_col and row.get(category_col) else None
        txn_currency = None
        txn_fx_rate = None
        if currency_col and row.get(currency_col):
            txn_currency = row[currency_col].strip().upper() or None
        if fx_rate_col and row.get(fx_rate_col):
            fx_str = normalize_amount(row[fx_rate_col].strip())
            if fx_str:
                try:
                    txn_fx_rate = Decimal(fx_str)
                except Exception:
                    pass

        txn_payee = row[payee_col].strip() if payee_col and row.get(payee_col) else None
        txn_external_id = row[external_id_col].strip() if external_id_col and row.get(external_id_col) else None
        txn_notes = row[notes_col].strip() if notes_col and row.get(notes_col) else None

        transactions.append(TransactionImport(
            description=row[desc_col].strip(),
            amount=abs(amount),
            date=txn_date,
            type=txn_type,
            currency=txn_currency,
            fx_rate=txn_fx_rate,
            category_name=category_name,
            payee_raw=txn_payee,
            external_id=txn_external_id,
            notes=txn_notes,
        ))

    return transactions


async def enrich_with_category_suggestions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transactions: list[TransactionImport],
) -> list[TransactionImport]:
    result = await session.execute(
        select(Rule)
        .where(Rule.workspace_id == workspace_id, Rule.is_active == True)
        .order_by(Rule.priority, Rule.id)
    )
    rules = result.scalars().all()

    category_result = await session.execute(
        select(Category).where(Category.workspace_id == workspace_id)
    )
    category_name_map = {str(c.id): c.name for c in category_result.scalars()}

    if not rules:
        return transactions

    for txn in transactions:
        proxy: Transaction = Transaction(
            description=txn.description,
            amount=txn.amount,
            date=txn.date,
            type=txn.type,
            account_id=None,
            payee_id=None,
            notes=None,
            category_id=None,
        )
        category_set = False
        for rule in rules:
            conditions = rule.conditions or []
            actions = rule.actions or []
            if evaluate_conditions(rule.conditions_op, conditions, proxy):
                category_set = apply_rule_actions(actions, proxy, category_set)
        if proxy.category_id:
            txn.suggested_category_id = proxy.category_id
            txn.suggested_category_name = category_name_map.get(str(proxy.category_id))

    return transactions


async def import_transactions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    transactions: list[TransactionImport],
    source: str,
    filename: str = "",
    detected_format: str = "",
    detect_duplicates: bool = True,
) -> tuple[int, int, int, uuid.UUID]:
    """Import transactions into an account in the given workspace.

    `workspace_id` scopes tenant filters + stamps new rows. `user_id`
    is the creator/author recorded on Transaction + ImportLog.
    Returns (imported, skipped, excluded, import_log_id)."""
    from app.models.import_log import ImportLog

    included = [t for t in transactions if not t.excluded]
    excluded_count = len(transactions) - len(included)

    # Calculate summaries from included transactions only
    total_credit = sum(t.amount for t in included if t.type == "credit")
    total_debit = sum(t.amount for t in included if t.type == "debit")

    # Create import log first to get its ID
    import_log = ImportLog(
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=account_id,
        filename=filename,
        format=detected_format,
        transaction_count=len(included),
        total_credit=total_credit,
        total_debit=total_debit,
    )
    session.add(import_log)
    await session.flush()  # Get the import_log.id

    # Look up account currency for fallback
    account_result = await session.execute(
        select(Account).where(Account.id == account_id)
    )
    account = account_result.scalar_one_or_none()
    account_currency = account.currency if account else get_settings().default_currency

    # Build category name → id map scoped to the workspace.
    category_result = await session.execute(
        select(Category).where(Category.workspace_id == workspace_id)
    )
    category_map = {c.name: c.id for c in category_result.scalars()}

    imported = 0
    skipped = 0
    effective_format = (detected_format or source or "").lower()
    should_detect_duplicates = detect_duplicates if effective_format == "csv" else True

    for txn_data in included:
        # Resolve currency: CSV value > account currency
        txn_currency = txn_data.currency or account_currency

        if should_detect_duplicates:
            # Prefer an external ID (OFX FITID), with date retained because some
            # Brazilian cards reuse one purchase FITID across monthly installments.
            # Formats without unique IDs fall back to transaction fields; compare
            # both descriptions because rules may have changed the displayed one.
            if txn_data.external_id:
                existing = await session.execute(
                    select(Transaction).where(
                        Transaction.account_id == account_id,
                        Transaction.external_id == txn_data.external_id,
                        Transaction.date == txn_data.date,
                    )
                )
            else:
                existing = await session.execute(
                    select(Transaction).where(
                        Transaction.account_id == account_id,
                        Transaction.date == txn_data.date,
                        Transaction.amount == txn_data.amount,
                        Transaction.type == txn_data.type,
                        or_(
                            Transaction.description == txn_data.description,
                            Transaction.original_description == txn_data.description,
                        ),
                    )
                )
            # `.first()` is intentional: duplicate keys can legitimately match
            # multiple rows after an import/sync race or reused bank identifier,
            # and duplicate detection only needs to establish that any row exists.
            if existing.scalars().first() is not None:
                skipped += 1
                continue

        import_payee_id = None
        import_payee_raw = getattr(txn_data, "payee_raw", None)
        if import_payee_raw:
            import_payee_entity = await get_or_create_payee(
                session, user_id, import_payee_raw, workspace_id=workspace_id,
                source="import",
            )
            import_payee_id = import_payee_entity.id

        user_category_id = txn_data.category_id
        suggested_cat_id = txn_data.suggested_category_id
        csv_category_id = (
            category_map.get(txn_data.category_name)
            if txn_data.category_name
            else None
        )
        category_id = (
            None
            if txn_data.force_uncategorized
            else user_category_id or suggested_cat_id or csv_category_id
        )

        incoming = Transaction(
            user_id=user_id,
            workspace_id=workspace_id,
            account_id=account_id,
            description=txn_data.description,
            original_description=txn_data.description,
            amount=txn_data.amount,
            date=txn_data.date,
            type=txn_data.type,
            source=source,
            import_id=import_log.id,
            external_id=txn_data.external_id,
            currency=txn_currency,
            payee=import_payee_raw,
            payee_id=import_payee_id,
            category_id=category_id,
            notes=getattr(txn_data, "notes", None),
        )
        apply_effective_date(incoming, account)
        preview = await preview_rules_for_transaction(
            session,
            user_id,
            incoming,
            skip_category_rules=txn_data.force_uncategorized,
        )

        # Normalize a detached candidate before either recurring match. If a
        # generated placeholder already represents this occurrence, upgrade it
        # in place; otherwise link the new row to an active recurring definition.
        placeholder = await recurring_match_service.find_placeholder_for_incoming(
            session,
            account_id,
            txn_data.amount,
            txn_currency,
            txn_data.type,
            txn_data.date,
            preview.description,
        )
        if placeholder and not placeholder.is_ignored:
            placeholder.source = source
            placeholder.external_id = txn_data.external_id
            placeholder.import_id = import_log.id
            placeholder.status = "posted"
            # The rules already ran, against the incoming charge, to build
            # `preview`. Fold that result in rather than re-running them
            # against the placeholder: its description is the recurring
            # definition's own wording, so conditions written for the bank's
            # text would no longer match. Everything the user can already see
            # wins, the charge only fills what is still empty, and only its
            # provenance is recorded outright.
            placeholder.original_description = txn_data.description
            if placeholder.category_id is None:
                placeholder.category_id = preview.category_id
            if import_payee_raw and not placeholder.payee:
                placeholder.payee = import_payee_raw
            if placeholder.payee_id is None:
                placeholder.payee_id = preview.payee_id
            placeholder.notes = merge_notes(placeholder.notes, preview.notes)
            if preview.is_ignored:
                placeholder.is_ignored = True
            imported += 1
            continue

        recurring_link = await recurring_match_service.find_bill_for_incoming(
            session,
            user_id,
            account_id,
            txn_data.amount,
            txn_currency,
            txn_data.type,
            txn_data.date,
            preview.description,
        )
        incoming.recurring_transaction_id = (
            recurring_link.id if recurring_link else None
        )
        if txn_data.fx_rate:
            incoming.fx_rate_used = txn_data.fx_rate
            incoming.amount_primary = txn_data.amount * txn_data.fx_rate

        session.add(incoming)
        await session.flush()
        if recurring_link is not None:
            recurring_match_service.advance_past(recurring_link, txn_data.date)

        await apply_rules_to_transaction(
            session,
            user_id,
            incoming,
            skip_category_rules=txn_data.force_uncategorized,
        )

        if not txn_data.fx_rate:
            await stamp_primary_amount(session, user_id, incoming)

        imported += 1

    # Update import log with actual imported count
    import_log.transaction_count = imported

    await session.commit()
    return imported, skipped, excluded_count, import_log.id

# Currency symbols and ISO codes around a number ("R$", "$", "NGN ", " EUR").
_AMOUNT_EDGE_LEADING = re.compile(r'^[^\d\-+(),.]+')
_AMOUNT_EDGE_TRAILING = re.compile(r'[^\d\-+(),.]+$')
_DR_CR_SUFFIX = re.compile(r'(?i)(?<![a-z])(dr|cr)\.?$')
# U+2212 minus, U+2012 figure dash, U+2013 en dash, U+FE63 small and
# U+FF0D fullwidth hyphen-minus: spreadsheet exports use them as a minus.
_UNICODE_MINUS = str.maketrans({c: '-' for c in '\u2212\u2012\u2013\ufe63\uff0d'})


def _strip_amount_decorations(amount_str: str) -> tuple[str, bool]:
    """Remove currency symbols, codes, whitespace and sign markers.

    Returns the bare number and whether it was negative. A leading minus
    (ASCII or a Unicode minus sign), accounting parentheses, e.g. "(12.50)",
    or a trailing "DR" mark a negative amount; a trailing "CR" marks a
    positive one.
    """
    s = re.sub(r"[\s'\u00a0\u202f]", "", amount_str).translate(_UNICODE_MINUS)
    # A standalone DR/CR suffix carries the sign, so read it before the edge
    # strip below would drop it as a currency code. Codes such as "XDR" or
    # "CRC" do not match.
    marker = _DR_CR_SUFFIX.search(s)
    if marker:
        s = s[: marker.start()]
    negative = False
    while True:
        before = s
        s = _AMOUNT_EDGE_LEADING.sub("", s)
        s = _AMOUNT_EDGE_TRAILING.sub("", s)
        if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
            negative = not negative
            s = s[1:-1]
        elif s[:1] == "-":
            negative = not negative
            s = s[1:]
        elif s[:1] == "+":
            s = s[1:]
        if s == before:
            if marker:
                negative = marker.group(1).lower() == "dr"
            return s, negative


def normalize_amount(amount_str: str | None) -> str:
    """
    Normalize monetary string into a standard decimal format compatible with Decimal.

    Currency symbols and codes around the number are dropped, and accounting
    parentheses read as a negative amount.

    Example:
        1.442,20 -> 1442.20
        1,442.20 -> 1442.20
        -R$ 1.234,56 -> -1234.56
    """
    if not amount_str:
        return ""

    amount_str, negative = _strip_amount_decorations(str(amount_str))
    if not amount_str:
        return ""

    if ',' in amount_str and '.' in amount_str:
        if amount_str.rfind(',') > amount_str.rfind('.'):
            amount_str = amount_str.replace('.', '').replace(',', '.')
        else:
            amount_str = amount_str.replace(',', '')
    elif ',' in amount_str:
        amount_str = amount_str.replace(',', '.')

    return f"-{amount_str}" if negative else amount_str
