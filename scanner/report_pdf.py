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
def _img(path: Path, width: float) -> Optional[RLImage]:
    """Scaled to `width`, keeping the source aspect ratio."""
    if not path.exists():
        return None
    try:
        iw, ih = ImageReader(str(path)).getSize()
        return RLImage(str(path), width=width, height=width * (ih / iw))
    except Exception as exc:
        log.warning("Could not embed %s in the PDF: %s", path.name, exc)
        return None


def _image_grid(entries: List[tuple], base: Path, per_row: int = 2,
                gutter: float = 8) -> List:
    """entries: [(caption, filename)]. Returns a list of flowables."""
    cell_w = (CONTENT_W - gutter * (per_row - 1)) / per_row
    cells = []
    for caption, filename in entries:
        picture = _img(base / filename, cell_w)
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

    flow = _section("Current Condition")
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

        # Before / after, side by side. The web report has the slider; print gets a pair.
        pair = [("BEFORE", reno.get("before")), ("AFTER", v.get("after"))]
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


# ── Entry point ──────────────────────────────────────────────────────────────
def build_pdf(output_path: Path, payload: dict, image_dir: Path) -> Path:
    """Render `payload` (the run_scan result) to a PDF at `output_path`."""
    doc = _Doc(str(output_path), address=payload.get("address", "Property"))

    story: List = []
    story += _cover(payload, image_dir)
    # The cover carries no footer; every page after it does.
    story += [NextPageTemplate("main")]

    for section in (_condition(payload),
                    _perspectives(payload, image_dir),
                    _temporal(payload, image_dir),
                    _renovation(payload, image_dir)):
        if section:
            story += [PageBreak()] + section

    story += [
        Spacer(1, 14), _rule(0, 6),
        _p("Generated by NexGen Property Intelligence from public satellite and Street View "
           "imagery together with any photographs supplied with the scan. Every finding "
           "here is read from those images and limited by what they show. Condition "
           "scores and renovation scopes are AI-generated assessments for screening "
           "purposes only — not a survey, an appraisal, or a contractor quote. No figure "
           "in this report is a valuation, a price or a return: photographs cannot "
           "support those, so none are offered.", "small"),
    ]

    doc.build(story)
    return output_path
