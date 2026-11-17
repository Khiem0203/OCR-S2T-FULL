"""Tách thông tin xuất bản ở trang cuối sách bằng regex."""
import re
import time
import unicodedata

FIELDS = ["nha_xuat_ban", "ban_bien_tap", "isbn", "luu_chieu", "luu_chieu_thang", "luu_chieu_nam",
          "giam_doc_tong_bien_tap", "che_ban", "trinh_bay_bia", "dia_chi_nxb", "dien_thoai_nxb",
          "sdt_quan_ly_xuat_ban", "sdt_tong_bien_tap", "fax_nxb", "dia_chi_co_so_in", "ngay_xac_nhan_dkxb"]
NUMBER_FIELDS = ("luu_chieu_thang", "luu_chieu_nam")

# Regex chạy trên text đã bỏ dấu, chữ thường
_LABEL = re.compile(
    r"^(?:chiu trach nhiem|pho giam doc|pho tong bien tap|uy vien|sua ban in|doc sach mau|doc mau|quyet dinh|"
    r"email|e-mail|website|in xong|nop luu chieu|chi nhanh|so dang ky|so xac nhan|giay dang ky|xac nhan dang ky|"
    r"ma so|isbn|dia chi|tru so|dien thoai|dt\b|tel\b|fax\b|nha xuat ban|nxb\b|quan ly xuat ban|tong bien tap|"
    r"(?:ban )?bien tap|trinh bay|thiet ke|ve bia|bia\s*[:|]|che ban|giam doc|in\b)")
_NAME_LABELS = {
    "ban_bien_tap": re.compile(r"^(?:ban )?bien tap(?: noi dung| vien| ky thuat)?\s*[:|]\s*"),
    "trinh_bay_bia": re.compile(r"^(?:(?:trinh bay|thiet ke|ve) bia|bia)\s*[:|]\s*"),
    "che_ban": re.compile(r"^che ban(?: vi tinh| dien tu)?\s*[:|]\s*"),
}
_DIRECTOR = re.compile(r"^giam doc\s*(?:[-–—,]|kiem)?\s*tong bien tap\s*[:|]?\s*")
_PUBLISHER = re.compile(r"^(?:nha xuat ban|nxb)\b\s*[:|]?\s*")
_ADDRESS = re.compile(r"(?:^|[^a-z])(?:dia chi|tru so)\s*[:|]\s*")
_PRINTER = re.compile(r"^in\b[^:]*\b(?:cuon|tai)\b|cong ty|xi nghiep|nha in|co so in")
_ISBN = re.compile(r"(?:\bisbn|^ma so(?: sach)?)\b[^0-9\n]{0,15}([0-9][0-9xX\-–. ]{2,})")
_PHONE = re.compile(r"\(?\+?\d[\d .()\-]{5,}\d")
_PHONE_END = re.compile(r"fax|email|e-mail|website|quan ly|tong bien tap|dien thoai|\bdt\b")
_DKXB = re.compile(r"dang ky(?: ke hoach)? xuat ban|dkxb|xac nhan dang ky")
_DATE = re.compile(r"ngay\s*:?\s*(\d{1,2})\s*(?:[/.\-]|thang)\s*(\d{1,2})\s*(?:[/.\-]|nam)\s*(\d{4})")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_INFO_PAGE = re.compile(r"chiu trach nhiem xuat ban|in xong|nop luu chieu")
_PUBLISHER_BLOCK = re.compile(r"^(?:nha xuat ban|nxb\b|dia chi|tru so|dien thoai|dt\b|tel\b|fax\b|email|e-mail|website|"
                              r"quan ly xuat ban|tong bien tap|chi nhanh|van phong)")

# Lưu chiểu: nhận cả lỗi font / OCR như 'lou chieau'
_DEPOSIT = re.compile(r"in xong|l\w{0,2}u chi\w{0,3}u")
_SEP = r"\s*(?:nam|[/.,\-–—])\s*"
_MONTH_YEAR = re.compile(r"(?:\bthang|\bt)\s*\.?\s*(\d{1,2})" + _SEP + r"(\d{4}|\d{2})(?!\d)")
_MONTH_WORDS = {"mot": 1, "gieng": 1, "hai": 2, "ba": 3, "tu": 4, "bon": 4, "nam": 5, "sau": 6, "bay": 7, "tam": 8,
                "chin": 9, "muoi": 10, "muoi mot": 11, "muoi hai": 12, "chap": 12}
_MONTH_WORD = re.compile(r"\bthang\s+(muoi mot|muoi hai|muoi|gieng|chap|mot|hai|ba|tu|bon|sau|bay|tam|chin|nam(?=\s+nam\b))"
                         r"\s*(?:nam\b)?\s*(\d{4})(?!\d)")
_FIRST_NUMBERS = re.compile(r"\D*?(?<!\d)(\d{1,2})\s*[/.\-–—]\s*(\d{4})(?!\d)")
# Quyết định xuất bản: dùng khi dòng In xong không có tháng + năm
_QDXB = re.compile(r"quyet dinh xuat ban|\bqdxb\b|\bqd\s*[-/]\s*nxb|\bnxb\w*\s*[-/]\s*qd\b")
_QD_DATE = re.compile(r"\bngay\s*:?\s*(\d{1,2})\s*(?:[/.\-–]|thang)\s*(\d{1,2})\s*(?:[/.\-–]|nam)\s*(\d{4}|\d{2})(?!\d)")


def plain(s: str) -> str:
    """Bỏ dấu, chữ thường, giữ nguyên độ dài."""
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn").lower()


def _lines(text: str) -> tuple[list, list]:
    lines = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"\s+", " ", unicodedata.normalize("NFC", ln).strip(" \t|"))
        if ln:
            lines.append(ln)
    return lines, [plain(ln) for ln in lines]


def _year4(y: str) -> int:
    y = int(y)
    if y >= 100:
        return y
    return 2000 + y if 2000 + y <= time.localtime().tm_year + 1 else 1900 + y


def _after(line: str, norm: str, pos: int) -> str:
    """Phần text gốc sau nhãn."""
    if len(line) != len(norm):
        pos = line.find(":") + 1 if ":" in line else len(line)
    return line[pos:].strip(" :|.;,")


def _names_below(lines: list, norms: list, i: int, limit: int) -> list:
    out = []
    for j in range(i + 1, min(len(lines), i + 1 + limit)):
        if _LABEL.match(norms[j]) or re.search(r"\d", norms[j]) or len(lines[j]) > 60:
            break
        out.append(lines[j].strip(" .;,"))
    return out


def _join_wrapped(lines: list, i: int, value: str) -> str:
    """Nối dòng bị xuống dòng giữa chừng."""
    j = i + 1
    while value and j < len(lines) and lines[j][:1].islower():
        value = f"{value} {lines[j]}".strip(" .;,")
        j += 1
    return value


def _phone(norms: list, label: str, skip: str = "") -> str:
    rx = re.compile(rf"(?:^|[^a-z])(?:{label})\s*[:.]?\s*")
    for n in norms:
        if skip and re.search(skip, n):
            continue
        for m in rx.finditer(n):
            part = n[m.end():]
            end = _PHONE_END.search(part)
            numbers = [s.strip() for s in _PHONE.findall(part[:end.start()] if end else part)]
            if numbers:
                return "; ".join(numbers)
    return ""


def month_years(part: str) -> list:
    """Các cặp (tháng, năm) hợp lệ, theo thứ tự xuất hiện."""
    found = [(m.start(), int(m.group(1)), _year4(m.group(2))) for m in _MONTH_YEAR.finditer(part)]
    found += [(m.start(), _MONTH_WORDS[m.group(1)], int(m.group(2))) for m in _MONTH_WORD.finditer(part)]
    if not found and (m := _FIRST_NUMBERS.match(part)):
        found.append((m.start(1), int(m.group(1)), int(m.group(2))))
    return [(t, y) for _, t, y in sorted(found) if 1 <= t <= 12 and 1900 <= y <= 2100]


def _qdxb_date(lines: list, norms: list):
    """Ngày Quyết định xuất bản -> (tháng, năm, câu) hoặc None."""
    out = None
    for i, n in enumerate(norms):
        m = _QDXB.search(n)
        if not m:
            continue
        part, sentence = n[m.start():], lines[i]
        if not _QD_DATE.search(part) and i + 1 < len(norms) and _QD_DATE.search(norms[i + 1]):
            part, sentence = f"{part} {norms[i + 1]}", f"{lines[i]} {lines[i + 1]}"
        d = _QD_DATE.search(part)
        if d and 1 <= int(d.group(1)) <= 31 and 1 <= int(d.group(2)) <= 12 and 1900 <= _year4(d.group(3)) <= 2100:
            out = (int(d.group(2)), _year4(d.group(3)), sentence)
    return out


def _deposit(lines: list, norms: list) -> tuple:
    """(câu, tháng, năm) lưu chiểu; không có tháng + năm thì lấy ngày Quyết định xuất bản."""
    sentence, month, year = "", None, None
    for i, (line, n) in enumerate(zip(lines, norms)):
        k = _DEPOSIT.search(n)
        if not k:
            continue
        part = n[k.start():]
        # Dòng chưa có năm thì nối dòng sau
        if not _YEAR.search(part) and i + 1 < len(norms) and not _DEPOSIT.search(norms[i + 1]) and _YEAR.search(norms[i + 1]):
            part, line = f"{part} {norms[i + 1]}", f"{line} {lines[i + 1]}"
        if pairs := month_years(part):
            return line, *pairs[0]
        if years := _YEAR.findall(part):
            sentence, year = line, int(years[-1])
        elif not sentence:
            sentence = line
    if qd := _qdxb_date(lines, norms):
        return qd[2], qd[0], qd[1]
    return sentence, month, year


def extract(text: str) -> dict:
    """Các trường thông tin xuất bản từ text trang cuối."""
    lines, norms = _lines(text)
    out = {k: "" for k in FIELDS}
    names = {k: [] for k in _NAME_LABELS}
    for i, (line, n) in enumerate(zip(lines, norms)):
        for key, rx in _NAME_LABELS.items():
            if m := rx.match(n):
                v = _after(line, n, m.end())
                names[key] += ([v] if v else []) + _names_below(lines, norms, i, 5)
                break

        m = _DIRECTOR.match(n)
        if m and not out["giam_doc_tong_bien_tap"]:
            v = _after(line, n, m.end())
            if not v or re.search(r"\d", v):
                v = (_names_below(lines, norms, i, 1) or [""])[0]
            out["giam_doc_tong_bien_tap"] = v

        m = _PUBLISHER.match(n)
        if m and not out["nha_xuat_ban"] and len(n) > m.end():
            v = _after(line, n, m.end()) if ":" in line[:m.end() + 1] else line.strip(" .;,")
            out["nha_xuat_ban"] = _join_wrapped(lines, i, v)

        if m := _ADDRESS.search(n):
            v = _join_wrapped(lines, i, _after(line, n, m.end()))
            near = range(max(0, i - 3), i + 1)
            printer = any(_PRINTER.search(norms[j]) for j in near) and not any(_PUBLISHER.match(norms[j]) for j in near)
            key = "dia_chi_co_so_in" if printer else "dia_chi_nxb"
            if v and not out[key]:
                out[key] = v

        m = _ISBN.search(n)
        if m and not out["isbn"]:
            out["isbn"] = re.sub(r"\s+", "", m.group(1)).strip(".")

        if _DKXB.search(n) and not out["ngay_xac_nhan_dkxb"]:
            nxt = " " + norms[i + 1] if i + 1 < len(norms) and norms[i + 1].startswith("ngay") else ""
            m = _DATE.search(n + nxt)
            if m and 1 <= int(m.group(2)) <= 12:
                out["ngay_xac_nhan_dkxb"] = f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"

    for key, found in names.items():
        out[key] = "; ".join(dict.fromkeys(x for x in found if x))
    out["dien_thoai_nxb"] = _phone(norms, r"dien thoai|dt|tel", r"quan ly xuat ban|tong bien tap|bien tap")
    out["fax_nxb"] = _phone(norms, r"fax")
    out["sdt_quan_ly_xuat_ban"] = _phone(norms, r"quan ly xuat ban(?:\s*[:.]?\s*(?:dt|dien thoai))?")
    out["sdt_tong_bien_tap"] = _phone(norms, r"tong bien tap(?:\s*[:.]?\s*(?:dt|dien thoai))?")
    out["luu_chieu"], out["luu_chieu_thang"], out["luu_chieu_nam"] = _deposit(lines, norms)
    return out


def count(fields: dict) -> int:
    """Số trường có giá trị (không tính tháng, năm tách riêng)."""
    return sum(bool(fields.get(k)) for k in FIELDS if k not in NUMBER_FIELDS)


def info_block(text: str) -> str:
    """Cắt từ khối NXB phía trên dòng Chịu trách nhiệm xuất bản trở xuống."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    idx = [i for i, ln in enumerate(lines) if "chiu trach nhiem xuat ban" in plain(ln)]
    if not idx:
        return "\n".join(lines)
    start = idx[-1]
    while start > 0 and _PUBLISHER_BLOCK.search(plain(lines[start - 1].strip())):
        start -= 1
    return "\n".join(lines[start:])


def is_info_page(text: str) -> bool:
    return bool(_INFO_PAGE.search(plain(text or "")))
