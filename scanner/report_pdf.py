"""
Downloadable PDF report.

ReportLab rather than an HTML-to-PDF converter: it is pure Python, so the
Railway image needs no headless browser and no system libraries, and the build
stays a plain `pip install`.

The PDF is print-first — dark ink on white — deliberately unlike the dark web
report, because these get printed and emailed to clients.
"""

import logging
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image as RLImage,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────
INK = colors.HexColor("#111827")
INK_2 = colors.HexColor("#4b5563")
INK_3 = colors.HexColor("#9ca3af")
RULE = colors.HexColor("#e5e7eb")
ACCENT = colors.HexColor("#1d4ed8")
POS = colors.HexColor("#15803d")
NEG = colors.HexColor("#b91c1c")
WARN = colors.HexColor("#a16207")
BAND = colors.HexColor("#f8fafc")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


# ── Styles ───────────────────────────────────────────────────────────────────
def _styles():
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold",
                                fontSize=26, leading=30, textColor=INK, alignment=0,
                                spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=13,
                                   leading=17, textColor=INK_2, spaceAfter=2),
        "eyebrow": ParagraphStyle("eyebrow", fontName="Helvetica-Bold", fontSize=8,
                                  leading=11, textColor=INK_3, spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=15, leading=19,
                             textColor=INK, spaceBefore=4, spaceAfter=8),
        "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=11.5, leading=15,
                             textColor=INK, spaceBefore=2, spaceAfter=4),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13.5,
                               textColor=INK_2, spaceAfter=4),
        "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=11,
                                textColor=INK_3),
        "caption": ParagraphStyle("caption", fontName="Helvetica-Bold", fontSize=7.5,
                                  leading=10, textColor=INK_3, alignment=TA_CENTER,
                                  spaceBefore=3),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=11.5,
                               textColor=INK_2),
        "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.5,
                                leading=11.5, textColor=INK),
        "kpi": ParagraphStyle("kpi", fontName="Helvetica-Bold", fontSize=17, leading=20,
                              textColor=INK),
        "kpilabel": ParagraphStyle("kpilabel", fontName="Helvetica-Bold", fontSize=7,
                                   leading=10, textColor=INK_3),
    }
    return s


ST = _styles()


def _p(text, style="body") -> Paragraph:
    return Paragraph(escape(str(text if text not in (None, "") else "—")), ST[style])


def _raw(html: str, style="body") -> Paragraph:
    """For the handful of places that need inline <b>/<font> markup."""
    return Paragraph(html, ST[style])


def _effort_color(level) -> colors.Color:
    """Light work reads green, substantial work reads amber — a scale of
    disruption, which is the one axis photographs can actually support."""
    return {"light": POS, "moderate": ACCENT, "substantial": WARN}.get(
        str(level).strip().lower(), INK_2)


def _title_case(key: str) -> str:
    return key.replace("_", " ").title()


def _kpi_value(text: str) -> Paragraph:
    """A KPI that shrinks rather than wraps — grades like 'A (Luxury/Pristine)'
    are long enough to break across two lines at the display size."""
    text = str(text)
    size = 17 if len(text) <= 9 else 13 if len(text) <= 16 else 10.5
    style = ParagraphStyle(f"kpi{size}", parent=ST["kpi"], fontSize=size,
                           leading=size * 1.18)
    return Paragraph(escape(text), style)


# ── Page furniture ───────────────────────────────────────────────────────────
class _Doc(BaseDocTemplate):
    """Adds the running footer. The cover page gets no footer."""

    def __init__(self, path: str, address: str, **kw):
        super().__init__(path, pagesize=A4,
                         leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN, bottomMargin=MARGIN + 6 * mm,
                         title=f"Property Intelligence Report — {address}",
                         author="NexGen Property Intelligence", **kw)
        self._address = address
        frame = Frame(MARGIN, MARGIN + 6 * mm, CONTENT_W,
                      PAGE_H - 2 * MARGIN - 6 * mm, id="body")
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame]),
            PageTemplate(id="main", frames=[frame], onPage=self._footer),
        ])

    def _footer(self, canvas, doc):
        canvas.saveState()
        y = MARGIN + 2 * mm
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, y + 8, PAGE_W - MARGIN, y + 8)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(INK_3)
        label = self._address if len(self._address) <= 70 else self._address[:67] + "..."
        canvas.drawString(MARGIN, y, label)
        canvas.drawRightString(PAGE_W - MARGIN, y, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()


def _rule(space_before=6, space_after=8) -> Table:
    t = Table([[""]], colWidths=[CONTENT_W], rowHeights=[0.6])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), space_before),
        ("BOTTOMPADDING", (0, 0), (-1, -1), space_after),
    ]))
    return t


def _section(title: str) -> List:
    return [Spacer(1, 6), _p(title, "h2"), _rule(0, 6)]


# ── Images ───────────────────────────────────────────────────────────────────
def _img(path: Path, width: float, max_height: Optional[float] = None) -> Optional[RLImage]:
    """Scaled to `width`, keeping the source aspect ratio, and no taller than
    `max_height` (default: most of a page) — a very tall photo would otherwise
    raise a LayoutError and lose the whole PDF."""
    if not path.exists():
        return None
    try:
        iw, ih = ImageReader(str(path)).getSize()
        if not iw or not ih:
            return None
        w, h = width, width * (ih / iw)
        cap = max_height or 150 * mm
        if h > cap:
            w, h = w * cap / h, cap
        return RLImage(str(path), width=w, height=h)
    except Exception as exc:
        log.warning("Could not embed %s in the PDF: %s", path.name, exc)
        return None


def _image_grid(entries: List[tuple], base: Path, per_row: int = 2,
                gutter: float = 8) -> List:
    """entries: [(caption, filename)]. Returns a list of flowables."""
    cell_w = (CONTENT_W - gutter * (per_row - 1)) / per_row
    cells = []
    for caption, filename in entries:
        picture = _img(base / filename, cell_w, max_height=min(cell_w * 1.6, 110 * mm))
        if picture is None:
            continue
        block = Table([[picture], [_p(caption, "caption")]], colWidths=[cell_w])
        block.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("BOX", (0, 0), (0, 0), 0.5, RULE),
        ]))
        cells.append(block)

    if not cells:
        return []

    rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    flow = []
    for row in rows:
        row = row + [""] * (per_row - len(row))
        grid = Table([row], colWidths=[cell_w] * per_row)
        grid.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), gutter / 2),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), gutter),
        ]))
        flow.append(grid)
    return flow


# ── Tables ───────────────────────────────────────────────────────────────────
def _facts_table(rows: List[tuple], width: float = CONTENT_W) -> Table:
    data = [[_p(label, "cellb"), _p(value, "cell")] for label, value in rows]
    t = Table(data, colWidths=[width * 0.38, width * 0.62])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _bullets(items: List[str], color=INK_2) -> List:
    if not items:
        return [_p("None noted", "small")]
    style = ParagraphStyle("bullet", parent=ST["body"], leftIndent=9,
                           bulletIndent=0, textColor=color, spaceAfter=2)
    return [Paragraph(escape(str(i)), style, bulletText="•") for i in items]


def _two_col(left: List, right: List, gutter: float = 12) -> Table:
    col = (CONTENT_W - gutter) / 2
    t = Table([[left, right]], colWidths=[col, col])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), gutter),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _callout(body: List, border=ACCENT, fill=colors.HexColor("#eff6ff")) -> Table:
    t = Table([[body]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


# ── Sections ─────────────────────────────────────────────────────────────────
def _cover(payload: dict, base: Path) -> List:
    if payload.get("health"):
        return _cover_v2(payload, base)
    inspection = payload.get("inspection") or {}
    arch = inspection.get("architectural_profile") or {}
    coords = payload.get("coordinates") or {}

    flow: List = [
        Spacer(1, 6 * mm),
        _p("PROPERTY INTELLIGENCE REPORT", "eyebrow"),
        _p(payload.get("address", "Unknown address"), "title"),
        _p(f"Scanned {_stamp(payload.get('scanned_at'))}"
           + (f"  ·  {coords['lat']:.5f}, {coords['lng']:.5f}" if coords else ""),
           "subtitle"),
        _rule(8, 10),
    ]

    grade = arch.get("overall_property_grade", "—")
    kpis = [
        ("OVERALL GRADE", str(grade)),
        ("CURB APPEAL", f"{arch.get('curb_appeal_score_1_to_10', '—')}/10"),
        ("ROOF CONDITION",
         f"{(inspection.get('roof_inspection') or {}).get('condition_score_1_to_10', '—')}/10"),
        ("IMAGERY CONFIDENCE", str(inspection.get("imagery_confidence", "—"))),
    ]
    cell_w = CONTENT_W / len(kpis)
    kpi_row = Table(
        [[_p(label, "kpilabel") for label, _ in kpis],
         [_kpi_value(value) for _, value in kpis]],
        colWidths=[cell_w] * len(kpis),
    )
    kpi_row.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, colors.white),
    ]))
    flow += [kpi_row, Spacer(1, 10)]

    confidence = str(inspection.get("imagery_confidence", "")).lower()
    if confidence and confidence != "high":
        flow += [
            _callout(
                [_raw(f"<b>Imagery confidence: {escape(str(inspection['imagery_confidence']))}.</b> "
                      "Facade-dependent findings in this report may be unsupported by the "
                      "available photography.", "body")],
                border=WARN, fill=colors.HexColor("#fefce8"),
            ),
            Spacer(1, 10),
        ]

    # Hero: the front elevation if we have it, else the satellite.
    imagery = payload.get("imagery") or {}
    hero_name = imagery.get("street_facade_front") or imagery.get("satellite_topdown")
    if hero_name:
        hero = _img(base / hero_name, CONTENT_W)
        if hero is not None:
            flow += [hero]

    return flow


def _stamp(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    return iso.replace("T", " ").replace("+00:00", " UTC")


def _condition(payload: dict) -> List:
    inspection = payload.get("inspection") or {}
    if not inspection:
        return []
    arch = inspection.get("architectural_profile") or {}
    roof = inspection.get("roof_inspection") or {}
    yard = inspection.get("yard_and_parcel") or {}

    left = [
        _p("Architecture & exterior", "h3"),
        _facts_table([
            ("Style", arch.get("primary_style")),
            ("Grade", arch.get("overall_property_grade")),
            ("Stories", arch.get("estimated_stories")),
            ("Curb appeal", f"{arch.get('curb_appeal_score_1_to_10', '—')}/10"),
            ("Garage", f"{arch.get('garage_capacity_estimated_cars', '—')} car(s)"),
            ("Siding", ", ".join(arch.get("exterior_siding_materials") or []) or "Unknown"),
        ], width=(CONTENT_W - 12) / 2),
    ]

    solar = ("Yes ({}% coverage)".format(roof.get("estimated_solar_coverage_pct", 0))
             if roof.get("solar_panels_detected") else "No")
    pool = (f"Yes ({yard.get('pool_type') or 'type unclear'})"
            if yard.get("swimming_pool_detected") else "None detected")
    right = [
        _p("Roof & parcel", "h3"),
        _facts_table([
            ("Roof", f"{roof.get('material', '—')} ({roof.get('shape', '—')})"),
            ("Condition", f"{roof.get('condition_score_1_to_10', '—')}/10"),
            ("Solar", solar),
            ("Pool", pool),
            ("Tree canopy", yard.get("tree_canopy_density")),
            ("Driveway", yard.get("driveway_type")),
        ], width=(CONTENT_W - 12) / 2),
    ]

    flow = _section("Exterior Survey" if payload.get("health") else "Current Condition")
    flow += [_two_col(left, right), Spacer(1, 12)]

    wear = roof.get("visible_wear_or_damage") or []
    structures = yard.get("accessory_structures") or []
    if wear or structures:
        flow += [_two_col(
            [_p("Visible roof wear", "h3")] + _bullets(wear),
            [_p("Accessory structures", "h3")] + _bullets(structures),
        ), Spacer(1, 12)]

    flow += [_two_col(
        [_p("Selling points", "h3")] + _bullets(inspection.get("key_selling_points") or [], POS),
        [_p("Risks & deferred maintenance", "h3")]
        + _bullets(inspection.get("risk_factors_or_deferred_maintenance") or [], NEG),
    )]
    return flow


def _perspectives(payload: dict, base: Path) -> List:
    imagery = payload.get("imagery") or {}
    if payload.get("photos"):
        imagery = {k: v for k, v in imagery.items() if not k.startswith("owner_photo")}
    if not imagery:
        return []
    entries = [
        (_title_case(name) + (" (supplied)" if name.startswith("owner_photo") else ""),
         filename)
        for name, filename in imagery.items()
    ]
    grid = _image_grid(entries, base, per_row=2)
    if not grid:
        return []

    flow = _section("Visual Perspectives")
    if any(name.startswith("owner_photo") for name in imagery):
        supplied = sum(1 for name in imagery if name.startswith("owner_photo"))
        flow += [_p(f"{supplied} photograph(s) supplied with this scan carried the same "
                    "weight as the Google imagery, and more where the two disagreed.",
                    "small"), Spacer(1, 6)]
    return flow + grid


def _temporal(payload: dict, base: Path) -> List:
    temporal = payload.get("temporal_analysis")
    timeline = payload.get("timeline") or []
    if not temporal:
        return []

    flow = _section("Temporal Analysis")

    if timeline:
        per_row = min(4, len(timeline))
        flow += _image_grid([(t["date"], t["file"]) for t in timeline], base,
                            per_row=per_row, gutter=6)
        flow += [Spacer(1, 6)]

    signals = [
        ("Roof replaced / repaired", "Yes" if temporal.get("roof_replaced_or_repaired") else "No"),
        ("Maintenance trajectory", temporal.get("maintenance_trajectory")),
        ("Vegetation trend", temporal.get("vegetation_trend")),
        ("New structures",
         ", ".join(temporal.get("new_structures_detected") or []) or "None detected"),
        ("Possible unpermitted work",
         ", ".join(temporal.get("possible_unpermitted_work") or []) or "None detected"),
    ]
    flow += [_facts_table(signals), Spacer(1, 8)]

    if temporal.get("upkeep_signal"):
        flow += [_callout([_raw("<b>Upkeep signal.</b> "
                                + escape(str(temporal["upkeep_signal"])), "body")]),
                 Spacer(1, 10)]

    snapshots = temporal.get("timeline") or []
    if snapshots:
        flow += [_p("Change log", "h3")]
        for snap in snapshots:
            block = [_p(snap.get("capture_date", "—"), "cellb"),
                     _p(snap.get("observed_state"), "cell")]
            block += _bullets(snap.get("changes_since_previous") or [])[:6] \
                if snap.get("changes_since_previous") else []
            entry = Table([[block]], colWidths=[CONTENT_W])
            entry.setStyle(TableStyle([
                ("LINEBEFORE", (0, 0), (0, -1), 2, ACCENT),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            flow += [KeepTogether(entry)]

    return flow


def _renovation(payload: dict, base: Path) -> List:
    reno = payload.get("renovation")
    if not reno:
        return []

    flow = _section("Renovation Concepts")
    flow += [
        _callout([
            _raw(f"<b>Recommended: {escape(str(reno.get('recommended_concept_name', '—')))}</b>",
                 "body"),
            _p(reno.get("recommendation_rationale"), "body"),
        ], border=POS, fill=colors.HexColor("#f0fdf4")),
        Spacer(1, 6),
    ]

    # Say out loud which photograph the renders were built from. When it is the
    # client's own, that is the single most reassuring line on the page.
    source = reno.get("before_source")
    if source:
        origin = ("your own photograph" if reno.get("before_is_owner_photo")
                  else "Street View imagery")
        flow += [_p(f"Concepts and renders were read from {origin} "
                    f"({_title_case(str(source))}).", "small")]
    flow += [Spacer(1, 8)]

    variants = reno.get("variants") or []
    for i, v in enumerate(variants):
        if i:
            flow += [PageBreak()]

        recommended = v.get("concept_name") == reno.get("recommended_concept_name")
        heading = escape(str(v.get("concept_name", "Concept")))
        if recommended:
            heading += ' <font size="7" color="#15803d">★ RECOMMENDED</font>'
        flow += [
            _raw(heading, "h2"),
            _p(f"{v.get('tier', '—')}  ·  targets {v.get('target_buyer', '—')}", "small"),
            Spacer(1, 5),
            _p(v.get("design_narrative"), "body"),
            Spacer(1, 6),
        ]

        # Current / concept, side by side — the same pairing the web report shows.
        pair = [("CURRENT", reno.get("before")), ("CONCEPT", v.get("after"))]
        pair = [(caption, filename) for caption, filename in pair if filename]
        if pair:
            flow += _image_grid(pair, base, per_row=max(2, len(pair)))
            flow += [Spacer(1, 4)]

        effort = v.get("overall_effort")
        read_from = [_title_case(str(x)) for x in (v.get("grounded_in_images") or [])]
        summary = [
            ("SCOPE TIER", str(v.get("tier") or "—"), INK),
            ("OVERALL EFFORT", str(effort or "—"), _effort_color(effort)),
            ("READ FROM", ", ".join(read_from) or "—", INK),
        ]
        cell_w = CONTENT_W / 3
        summary_row = Table(
            [[_p(label, "kpilabel") for label, _, _ in summary],
             [Paragraph(escape(value),
                        ParagraphStyle(f"sum{i}{n}", parent=ST["kpi"],
                                       fontSize=11 if len(value) > 16 else 14,
                                       leading=15 if len(value) > 16 else 17,
                                       textColor=color))
              for n, (_, value, color) in enumerate(summary)]],
            colWidths=[cell_w] * 3,
        )
        summary_row.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BAND),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("LINEAFTER", (0, 0), (-2, -1), 0.5, colors.white),
        ]))
        flow += [summary_row, Spacer(1, 6)]

        if v.get("visual_impact"):
            flow += [_callout([_raw("<b>What changes on sight.</b> "
                                    + escape(str(v["visual_impact"])), "body")]),
                     Spacer(1, 10)]

        # Every row carries the image evidence behind it, which is the whole
        # claim this report is willing to make: here is what we can see, and
        # here is the work it calls for.
        scope_items = v.get("scope_items") or []
        if scope_items:
            header = [_p("Scope item", "cellb"), _p("Effort", "cellb"),
                      _p("What the imagery shows", "cellb")]
            rows = [header]
            for item in scope_items:
                rows.append([
                    _p(item.get("scope_item"), "cell"),
                    Paragraph(escape(str(item.get("effort") or "—")),
                              ParagraphStyle(f"eff{i}", parent=ST["cellb"],
                                             textColor=_effort_color(item.get("effort")))),
                    _p(item.get("visual_evidence"), "cell"),
                ])
            table = Table(rows, colWidths=[CONTENT_W * 0.28, CONTENT_W * 0.13,
                                           CONTENT_W * 0.59], repeatRows=1)
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), BAND),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
                ("LINEBELOW", (0, 1), (-1, -2), 0.4, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (0, -1), 4),
            ]))
            flow += [table,
                     _p("Every item above is tied to something visible in the imagery. "
                        "Figures are deliberately absent - a contractor standing at the "
                        "building is the only honest source for those.", "small"),
                     Spacer(1, 10)]

        flow += [_two_col(
            [_p("Highest-leverage move", "h3"), _p(v.get("highest_leverage_move"), "body")],
            [_p("Key risk", "h3"), _p(v.get("key_risk"), "body")],
        )]

    return flow


# ── Inspection layer (version 2 payloads) ──────────────────────────────────
def _issues(payload: dict) -> List[dict]:
    """The issues still standing: the owner can mark one "not an issue"."""
    return [i for i in (payload.get("inspection_report") or {}).get("issues") or []
            if not i.get("dismissed_by_user")]


def _dismissed(payload: dict) -> List[dict]:
    return [i for i in (payload.get("inspection_report") or {}).get("issues") or []
            if i.get("dismissed_by_user")]


ORANGE = colors.HexColor("#c2410c")
SEV_COLOR = {"critical": NEG, "major": ORANGE, "minor": WARN, "cosmetic": INK_3}
SEV_LABEL = {"critical": "CRITICAL", "major": "MAJOR", "minor": "MINOR", "cosmetic": "COSMETIC"}
URG_LABEL = {"immediate": "Immediately", "within_6_months": "Within 6 months",
             "within_2_years": "Within 1-2 years", "monitor": "Monitor"}
STATUS_LABEL = {"past_life": "Past typical life", "replacement_window": "In replacement window",
                "within_life": "Within typical life", "dated": "Dated", "unknown": "Age unknown"}


def _score_color(score) -> colors.Color:
    if score is None:
        return INK_3
    return POS if score >= 75 else WARN if score >= 55 else NEG


def _labels(payload: dict) -> dict:
    """label -> how a person would name the photo ('Kitchen · P03')."""
    out = {}
    for p in payload.get("photos") or []:
        tag = {"owner": p.get("ref", ""), "satellite": "Satellite",
               "street_view": "Street View"}.get(p.get("source"), "")
        out[p["label"]] = f"{p.get('space_label') or _title_case(p['label'])} · {tag}"
    return out


def _hero(payload: dict) -> Optional[str]:
    listing = payload.get("listing") or {}
    order = listing.get("photo_order") or []
    photos = {p["label"]: p for p in payload.get("photos") or []}
    if order and order[0] in photos:
        return photos[order[0]]["file"]
    exterior = [p for p in photos.values() if p.get("source") == "owner" and p.get("is_exterior")]
    if exterior:
        return max(exterior, key=lambda p: p.get("listing_appeal", 0))["file"]
    imagery = payload.get("imagery") or {}
    return imagery.get("street_facade_front") or imagery.get("satellite_topdown") or (
        next(iter(photos.values()))["file"] if photos else None)


def _cover_v2(payload: dict, base: Path) -> List:
    health = payload.get("health") or {}
    report = payload.get("inspection_report") or {}
    coords = payload.get("coordinates") or {}
    counts = health.get("counts") or {}
    photos = payload.get("photos") or []
    owner = sum(1 for p in photos if p.get("source") == "owner")

    flow: List = [
        Spacer(1, 6 * mm),
        _p("PROPERTY INSPECTION REPORT", "eyebrow"),
        _p(payload.get("address", "Unknown address"), "title"),
        _p(f"Scanned {_stamp(payload.get('scanned_at'))}"
           + (f"  ·  {coords['lat']:.5f}, {coords['lng']:.5f}" if coords else "")
           + f"  ·  {owner} photo{'s' if owner != 1 else ''} supplied", "subtitle"),
        _rule(8, 10),
    ]

    score = health.get("score")
    score_text = f"{score}/100 · {health.get('grade', '—')}" if score is not None else "—"
    urgent = counts.get("critical", 0)
    kpis = [
        ("HEALTH SCORE" + (" (PROVISIONAL)" if health.get("provisional") else ""), score_text,
         _score_color(score)),
        ("ISSUES FOUND", str(len(_issues(payload))), INK),
        ("CRITICAL", str(urgent), NEG if urgent else POS),
        ("SYSTEMS ASSESSED",
         f"{health.get('systems_assessed', 0)} of {health.get('systems_total', 12)}", INK),
    ]
    cell_w = CONTENT_W / len(kpis)
    kpi_row = Table(
        [[_p(label, "kpilabel") for label, _, _ in kpis],
         [Paragraph(escape(value), ParagraphStyle(f"cv{n}", parent=ST["kpi"], textColor=tone,
                                                  fontSize=17 if len(value) < 12 else 13))
          for n, (_, value, tone) in enumerate(kpis)]],
        colWidths=[cell_w] * len(kpis),
    )
    kpi_row.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("LINEAFTER", (0, 0), (-2, -1), 0.5, colors.white),
    ]))
    flow += [kpi_row, Spacer(1, 10)]

    if report.get("verdict") or report.get("executive_summary"):
        body = []
        if report.get("verdict"):
            body.append(_raw(f"<b>{escape(report['verdict'])}</b>", "body"))
        if report.get("executive_summary"):
            body.append(_p(report["executive_summary"], "body"))
        dismissed = _dismissed(payload)
        if dismissed:
            # The summary is the model's, written before the owner's corrections;
            # say so rather than let it contradict the register.
            n = len(dismissed)
            body.append(_p(f"Written before the owner marked {n} item{'' if n == 1 else 's'} "
                           "not an issue. The score, register and action plan in this report "
                           "reflect those corrections.", "small"))
        flow += [_callout(body), Spacer(1, 10)]

    if health.get("provisional"):
        flow += [_callout([_p(
            f"Provisional score: the photographs gave evidence on "
            f"{health.get('systems_assessed', 0)} of {health.get('systems_total', 12)} building "
            "systems. The rest are listed at the end of this report with the photographs that "
            "would cover them.", "small")], border=WARN, fill=colors.HexColor("#fefce8")),
            Spacer(1, 10)]

    hero = _hero(payload)
    if hero:
        picture = _img(base / hero, CONTENT_W * 0.86)
        if picture is not None and picture.drawHeight > 120 * mm:
            ratio = (120 * mm) / picture.drawHeight
            picture.drawHeight *= ratio
            picture.drawWidth *= ratio
        if picture is not None:
            flow += [picture]
    return flow


def _bar(score, width: float = 70 * mm) -> Table:
    """A horizontal score bar, as a two-cell table."""
    if score is None:
        t = Table([[""]], colWidths=[width], rowHeights=[5])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), RULE)]))
        return t
    filled = max(0.02, min(1.0, score / 100)) * width
    t = Table([["", ""]], colWidths=[filled, max(0.01, width - filled)], rowHeights=[5])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), _score_color(score)),
        ("BACKGROUND", (1, 0), (1, 0), RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _inspection_summary(payload: dict) -> List:
    health = payload.get("health")
    if not health:
        return []
    flow = _section("Health by System")
    flow += [_p(health.get("method", ""), "small"), Spacer(1, 8)]

    rows = [[_p("System", "cellb"), _p("Score", "cellb"), _p("", "cellb"),
             _p("Issues", "cellb"), _p("Worst", "cellb")]]
    for sys in health.get("systems") or []:
        score = sys.get("score")
        worst = sys.get("worst")
        rows.append([
            _p(sys["label"], "cell"),
            Paragraph(escape(f"{score}" if score is not None else "Not assessed"),
                      ParagraphStyle("sc", parent=ST["cellb"], textColor=_score_color(score))),
            _bar(score, CONTENT_W * 0.28 - 10),
            _p(str(sys.get("issues", 0)) if sys.get("assessed") else "—", "cell"),
            Paragraph(escape(SEV_LABEL.get(worst, "—") if worst else "—"),
                      ParagraphStyle("wv", parent=ST["cellb"],
                                     textColor=SEV_COLOR.get(worst, INK_3))),
        ])
    table = Table(rows, colWidths=[CONTENT_W * 0.32, CONTENT_W * 0.15, CONTENT_W * 0.28,
                                   CONTENT_W * 0.10, CONTENT_W * 0.15], repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 4),
    ]))
    flow += [table, Spacer(1, 12)]

    trades = health.get("trades") or []
    report = payload.get("inspection_report") or {}
    flow += [_two_col(
        [_p("Who to call", "h3")] + _bullets([f"{t['trade']} — {t['issues']} issue"
                                              f"{'s' if t['issues'] != 1 else ''}"
                                              for t in trades[:8]]),
        [_p("In good order", "h3")] + _bullets(report.get("strengths") or [], POS),
    )]
    return flow


def _register(payload: dict, base: Path) -> List:
    issues = _issues(payload)
    dismissed = _dismissed(payload)
    if not payload.get("health"):
        return []
    flow = _section("Defect Register")
    if dismissed:
        flow += [_p("Marked not an issue by the owner, and left out of this register, the "
                    "action plan and the score: "
                    + "; ".join(f"{i['id']} {i['title']}" for i in dismissed) + ".", "small"),
                 Spacer(1, 8)]
    if not issues and dismissed:
        return flow + [_p("Every defect the inspection found has been marked not an issue by "
                          "the owner (listed above).", "body")]
    if not issues:
        return flow + [_p("No defects were found in the photographs supplied. That is a "
                          "statement about what was photographed, not a clean bill of "
                          "health — see the coverage section at the end.", "body")]

    crops = payload.get("crops") or {}
    names = _labels(payload)
    img_w = 58 * mm
    for issue in issues:
        sev = issue["severity"]
        head = (f'<font color="{SEV_COLOR[sev].hexval().replace("0x", "#")}"><b>'
                f'{SEV_LABEL[sev]}</b></font>  ·  {escape(URG_LABEL.get(issue["urgency"], ""))}'
                f'  ·  {escape(issue.get("trade") or "")}')
        text = [
            _raw(f"<b>{escape(issue['id'])}  {escape(issue['title'])}</b>", "h3"),
            _raw(head, "small"),
            Spacer(1, 3),
            _p(issue.get("summary"), "body"),
            _raw(f"<b>Next step:</b> {escape(issue.get('recommendation') or '—')}", "body"),
            _p("Seen in: " + ", ".join(names.get(l, l) for l in issue.get("photos") or []),
               "small"),
        ]
        picture = _img(base / crops[issue["id"]], img_w) if issue["id"] in crops else None
        if picture is not None and picture.drawHeight > 62 * mm:
            ratio = (62 * mm) / picture.drawHeight
            picture.drawHeight *= ratio
            picture.drawWidth *= ratio
        cells = [[picture or _p("No box drawn", "small"), text]]
        row = Table(cells, colWidths=[img_w + 6, CONTENT_W - img_w - 6])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, SEV_COLOR[sev]),
            ("LEFTPADDING", (0, 0), (0, -1), 6), ("LEFTPADDING", (1, 0), (1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        flow += [KeepTogether([row, Spacer(1, 10)])]
    return flow


def _action_plan(payload: dict) -> List:
    issues = _issues(payload)
    if not issues:
        return []
    flow = _section("Action Plan")
    for urgency in ("immediate", "within_6_months", "within_2_years", "monitor"):
        mine = [i for i in issues if i["urgency"] == urgency]
        if not mine:
            continue
        rows = [[_p(i["id"], "cellb"), _p(i["title"], "cell"), _p(i.get("trade"), "cell"),
                 Paragraph(SEV_LABEL[i["severity"]],
                           ParagraphStyle("ap", parent=ST["cellb"],
                                          textColor=SEV_COLOR[i["severity"]]))]
                for i in mine]
        table = Table(rows, colWidths=[CONTENT_W * 0.08, CONTENT_W * 0.52, CONTENT_W * 0.25,
                                       CONTENT_W * 0.15])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (0, -1), 0),
        ]))
        flow += [_p(URG_LABEL[urgency], "h3"), table, Spacer(1, 10)]

    questions = (payload.get("inspection_report") or {}).get("questions_to_ask") or []
    if questions:
        flow += [_p("Questions to ask", "h3")] + _bullets(questions)
    return flow


def _equipment(payload: dict) -> List:
    items = payload.get("equipment") or []
    if not items:
        return []
    flow = _section("Equipment & Data Plates")
    rows = [[_p("Equipment", "cellb"), _p("Make / model / serial", "cellb"),
             _p("Made", "cellb"), _p("Typical life", "cellb"), _p("Status", "cellb")]]
    for e in items:
        made = str(e["manufacture_year"]) if e.get("manufacture_year") else "—"
        if e.get("age_years") is not None:
            made += f" ({e['age_years']} yrs, {e.get('age_basis')})"
        life = (f"{e['life_low']}–{e['life_high']} yrs" if e.get("life_low") else "—")
        status = e.get("status", "unknown")
        tone = {"past_life": NEG, "replacement_window": WARN, "within_life": POS}.get(status, INK_3)
        rows.append([
            _p(e["kind"], "cellb"),
            _p(" / ".join(x for x in (e.get("brand"), e.get("model"), e.get("serial")) if x)
               or "Not legible", "cell"),
            _p(made, "cell"), _p(life, "cell"),
            Paragraph(escape(STATUS_LABEL.get(status, status)),
                      ParagraphStyle("st", parent=ST["cellb"], textColor=tone)),
        ])
    table = Table(rows, colWidths=[CONTENT_W * 0.2, CONTENT_W * 0.3, CONTENT_W * 0.2,
                                   CONTENT_W * 0.13, CONTENT_W * 0.17], repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 4),
    ]))
    return flow + [table, Spacer(1, 6),
                   _p("Years are transcribed from data plates or decoded from serial formats "
                      "where the basis says so; confirm before relying on them. Service lives "
                      "are typical industry ranges, not predictions for this unit.", "small")]


def _spaces(payload: dict, base: Path) -> List:
    report = payload.get("inspection_report") or {}
    spaces = report.get("spaces") or []
    photos = {p["label"]: p for p in payload.get("photos") or []}
    if not spaces or not photos:
        return []
    annotated = payload.get("annotated") or {}
    standing = {i["id"] for i in _issues(payload)}
    flow = _section("Rooms & Photo Log")
    for sp in spaces:
        sp = {**sp, "issues": [i for i in sp.get("issues") or [] if i in standing]}
        entries = []
        for label in sp["photos"]:
            p = photos.get(label)
            if not p:
                continue
            name = annotated.get(label) or p.get("thumb") or p.get("file")
            entries.append((f"{p.get('ref', '')} · {p.get('caption') or p.get('space_label')}"[:60],
                            name))
        head = [_raw(f"<b>{escape(sp['name'])}</b>  "
                     f'<font size="8" color="#9ca3af">{escape(sp.get("condition") or "")}'
                     f"{'  ·  ' + str(len(sp.get('issues') or [])) + ' issue(s)' if sp.get('issues') else ''}"
                     f"</font>", "h3"),
                _p(sp.get("summary"), "small"), Spacer(1, 4)]
        grid = _image_grid(entries, base, per_row=4, gutter=6)
        flow += [KeepTogether(head + grid[:1])] + grid[1:] + [Spacer(1, 8)]
    return flow


def _roof(payload: dict, base: Path) -> List:
    solar = payload.get("solar")
    if not solar:
        return []
    flow = _section("Roof Measurements & Solar Potential")
    if solar.get("likely_neighbour"):
        flow += [_callout([_p(f"The nearest building Google has measured is "
                              f"{solar.get('distance_m')} m from the geocoded point, so these "
                              "figures may describe a neighbouring roof.", "small")],
                          border=WARN, fill=colors.HexColor("#fefce8")), Spacer(1, 8)]
    half = (CONTENT_W - 12) / 2
    picture = _img(base / payload["satellite_roof"], half) \
        if payload.get("satellite_roof") else None
    configs = solar.get("configs") or []
    best = configs[-1] if configs else None
    facts = _facts_table([
        ("Roof area", f"{solar.get('roof_area_m2', 0):,.0f} m²"),
        ("Roof planes", str(len(solar.get("segments") or []))),
        ("Max panels", f"{solar.get('max_panels', 0)} × {solar['panel']['capacity_w']} W"),
        ("Max yearly output", f"{best[1]:,} kWh (DC)" if best else "—"),
        ("Peak sun", f"{solar.get('max_sunshine_hours', 0):,} h/yr"),
        ("Imagery", f"{solar.get('imagery_date') or '—'} ({solar.get('imagery_quality') or '—'})"),
    ], width=half if picture else CONTENT_W)
    flow += [_two_col([picture] if picture else [], [facts]) if picture else facts,
             Spacer(1, 10)]

    segs = solar.get("segments") or []
    if segs:
        rows = [[_p("Plane", "cellb"), _p("Pitch", "cellb"), _p("Faces", "cellb"),
                 _p("Area", "cellb"), _p("Median sun", "cellb")]]
        for n, seg in enumerate(segs, 1):
            rows.append([_p(str(n), "cell"), _p(f"{seg['pitch']}°", "cell"),
                         _p(f"{_compass(seg['azimuth'])} ({seg['azimuth']:.0f}°)", "cell"),
                         _p(f"{seg['area_m2']:,.0f} m²", "cell"),
                         _p(f"{seg['sunshine_median'] or 0:,.0f} h/yr", "cell")])
        table = Table(rows, colWidths=[CONTENT_W * 0.1] + [CONTENT_W * 0.225] * 4, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        flow += [table, Spacer(1, 6)]
    return flow + [_p("Measured by the Google Solar API from aerial imagery and elevation data. "
                      "Output is the modelled direct-current yield before inverter and wiring "
                      "losses.", "small")]


def _compass(azimuth: float) -> str:
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int(((azimuth % 360) + 22.5) // 45) % 8]


def _makeovers(payload: dict, base: Path) -> List:
    made = payload.get("makeovers") or []
    if not made:
        return []
    photos = {p["label"]: p for p in payload.get("photos") or []}
    flow = _section(f"Reimagined — {payload.get('style_name') or 'Makeovers'}")
    for i, m in enumerate(made):
        src = photos.get(m["source"])
        if not src:
            continue
        block = [_p(m.get("title", "").capitalize(), "h3")]
        if m.get("fixes"):
            block.append(_p("Also shown repaired: " + "; ".join(m["fixes"]), "small"))
        block += _image_grid([("CURRENT", src["file"]), ("REIMAGINED", m["after"])], base,
                             per_row=2)
        flow += [KeepTogether(block), Spacer(1, 8)]
    return flow + [_p("AI visualisations of a design direction, not construction drawings.",
                      "small")]


def _listing(payload: dict) -> List:
    listing = payload.get("listing")
    if not listing:
        return []
    flow = _section("Listing Kit")
    flow += [_p(listing.get("headline"), "h3"), _p(listing.get("description"), "body"),
             Spacer(1, 6)]
    flow += [_two_col(
        [_p("Highlights", "h3")] + _bullets(listing.get("highlights") or [], POS),
        [_p("Before the photographer comes", "h3")] + _bullets(listing.get("pre_listing_fixes")
                                                                or []),
    )]
    retake = listing.get("retake") or []
    if retake:
        names = _labels(payload)
        flow += [Spacer(1, 8), _p("Photos to retake", "h3")] + _bullets(
            [f"{names.get(r['photo'], r['photo'])}: {r['note']}" for r in retake])
    return flow


def _coverage(payload: dict) -> List:
    health = payload.get("health")
    if not health:
        return []
    report = payload.get("inspection_report") or {}
    gaps = report.get("coverage_gaps") or []
    unseen = [s for s in health.get("systems") or [] if not s.get("assessed")]
    if not gaps and not unseen:
        return []
    flow = _section("What the Photos Did Not Show")
    flow += [_p("A finding-free system is only as good as the photographs behind it. These "
                "were not covered; adding the shots below and running a new scan completes "
                "the picture.", "body"), Spacer(1, 6)]
    rows = [[_p("Area", "cellb"), _p("Why it matters", "cellb"), _p("Photograph to take", "cellb")]]
    for g in gaps:
        rows.append([_p(g["area"], "cellb"), _p(g.get("why"), "cell"), _p(g.get("shot"), "cell")])
    covered = {g["area"].lower() for g in gaps}
    for s in unseen:
        if s["label"].lower() not in covered:
            rows.append([_p(s["label"], "cellb"), _p("No photograph showed this system.", "cell"),
                         _p(s.get("shot"), "cell")])
    table = Table(rows, colWidths=[CONTENT_W * 0.24, CONTENT_W * 0.36, CONTENT_W * 0.4],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 4),
    ]))
    return flow + [table]


# ── Entry point ──────────────────────────────────────────────────────────────
def build_pdf(output_path: Path, payload: dict, image_dir: Path) -> Path:
    """Render `payload` (the run_scan result) to a PDF at `output_path`."""
    doc = _Doc(str(output_path), address=payload.get("address", "Property"))

    story: List = []
    story += _cover(payload, image_dir)
    # The cover carries no footer; every page after it does.
    story += [NextPageTemplate("main")]

    sections = (
        _inspection_summary(payload),
        _register(payload, image_dir),
        _action_plan(payload),
        _equipment(payload),
        _spaces(payload, image_dir),
        _roof(payload, image_dir),
        _condition(payload),
        _perspectives(payload, image_dir),
        _temporal(payload, image_dir),
        _makeovers(payload, image_dir),
        _renovation(payload, image_dir),
        _listing(payload),
        _coverage(payload),
    )
    for section in sections:
        if section:
            story += [PageBreak()] + section

    story += [
        Spacer(1, 14), _rule(0, 6),
        _p("Generated by NexGen Property Intelligence from public satellite and Street View "
           "imagery together with any photographs supplied with the scan. Every finding "
           "here is read from those images and limited by what they show; roof geometry "
           "and solar figures come from the Google Solar API where available. Condition "
           "scores, the health score and renovation scopes are AI-assisted assessments for "
           "screening purposes only — not a survey, an appraisal, a structural report or "
           "a contractor quote. Equipment ages are read from data plates and should be "
           "confirmed. No figure in this report is a valuation, a price or a return: "
           "photographs cannot support those, so none are offered.", "small"),
    ]

    doc.build(story)
    return output_path
