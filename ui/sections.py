import io
import re
import ast
import operator as _operator
import streamlit as st
import pandas as pd
from pathlib import Path
from config.containers import CONTAINERS


# ----------------------------------------------------
# Load costing Excel
# ----------------------------------------------------
@st.cache_data
def load_costing_data():
    base_path = Path(__file__).resolve().parent.parent
    cost_file = base_path / "assets" / "Container & Dry Van Costing Data.xlsx"
    container_cost_df = pd.read_excel(cost_file, sheet_name="Container_Cost")
    dry_van_cost_df = pd.read_excel(cost_file, sheet_name="Dry_Van")
    return container_cost_df, dry_van_cost_df


# ----------------------------------------------------
# Cost calculation
# ----------------------------------------------------
def calculate_total_cost(
    container_count,
    container_type,
    origin_city,
    destination_city,
    container_cost_df,
    dry_van_cost_df,
):
    if container_type == "53 Dry Van":
        row = dry_van_cost_df[
            (dry_van_cost_df["Origin City"] == origin_city)
            & (dry_van_cost_df["Destination City"] == destination_city)
        ]
    else:
        row = container_cost_df[
            (container_cost_df["Origin City"] == origin_city)
            & (container_cost_df["Destination City"] == destination_city)
            & (container_cost_df["Container Type"] == container_type)
        ]
    if row.empty:
        return None
    return float(row.iloc[0]["Total Cost"]) * container_count


# ----------------------------------------------------
# Download input template (ULTRA TIGHT)
# ----------------------------------------------------
DISPLAY_COLUMNS = [
    "Rack / Finished Good",
    "Quantity",
    "Length (MM)",
    "Width (MM)",
    "Height (MM)",
    "Weight (Kg)",
]

_NUMERIC_TEXT_COLS = ["Length (MM)", "Width (MM)", "Height (MM)", "Weight (Kg)"]

# ----------------------------------------------------
# Per-rack packaging & stackability options
# ----------------------------------------------------
# Packaging materials the operator can pick (used by the stacking engine to
# decide bearing capacity and the metal-on-corrugate rule).
PACKAGING_MATERIALS = [
    "Metal Rack",
    "Plastic Box",
    "Plastic Bin",
    "Plastic Pallet",
    "Wooden Pallet+Corrugate Box",
    "Metal Pallet+Corrugate Box",
    "Wooden Box",
    "Plywood Box",
]
ALL_MATERIALS = PACKAGING_MATERIALS

# Stackability choices:
#   "Auto"          -> stack as many as SAFELY fit (uses height + the 540 kg /
#                      metal-nesting capacity rules). This is the default so
#                      racks that can stack are stacked (minimises floor space).
#   "Non Stackable" -> never stack (box must stand alone).
#   "G+1" .. "G+20" -> exactly Ground + N high (G+1 = 2 levels, G+2 = 3, ...).
STACKABILITY_OPTIONS = ["Auto", "Non Stackable"] + [f"G+{i}" for i in range(1, 21)]

# Loading access / allowed loading direction. The labels use the container's
# own reference points (NOSE->DOORS = the long axis, SIDE->SIDE = the short
# axis) because words like "along"/"across" can be read either way:
#   4-Way : forklift can enter from any side -> the rack may be rotated (most
#           packages).
#   2-Way : the rack can only be picked from two sides, so its LENGTH must
#           point one specific way.
LOADING_ACCESS_OPTIONS = [
    "4-Way (any direction)",
    "Package length along container length",
    "Package length along container width",
]

# Sensible defaults for a fresh row.
# Packaging Material is left BLANK on purpose: it is MANDATORY, so the user must
# actively choose it (a wrong/blank material would give the wrong bearing
# capacity). Stackability defaults to "Auto" so stackable racks are stacked.
_PKG_DEFAULTS = {
    "Packaging Material": "",
    "Stackability": "Auto",
    "Loading Access": LOADING_ACCESS_OPTIONS[0],   # 4-Way is the common case
}
PACKAGING_COLUMNS = list(_PKG_DEFAULTS.keys())

# Full set of columns the input table works with (dimensions + packaging)
ALL_INPUT_COLUMNS = DISPLAY_COLUMNS + PACKAGING_COLUMNS


def render_download_template():
    df = pd.DataFrame(columns=ALL_INPUT_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Rack Input")
        ws = writer.sheets["Rack Input"]
        # Note on the Height (MM) header: use the FOLDED height if folded.
        try:
            from openpyxl.comments import Comment
            from openpyxl.utils import get_column_letter
            h_col = get_column_letter(ALL_INPUT_COLUMNS.index("Height (MM)") + 1)
            ws[f"{h_col}1"].comment = Comment(
                "Enter the FOLDED height here if the rack / package is shipped "
                "in folded condition.", "SmartPack")
            # Visible note a couple of rows below the header row too.
            note_row = 4
            ws.cell(row=note_row, column=1,
                    value=("NOTE: If a rack/package ships FOLDED, enter its "
                           "FOLDED height in the Height (MM) column."))
        except Exception:
            pass
    st.download_button(
        "[↓] Download Input Template",
        buf.getvalue(),
        "smartpack_input_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # Pull next section upward HARD
    st.markdown("<div style='margin-top:-34px'></div>", unsafe_allow_html=True)


# ----------------------------------------------------
# Calculator-style numeric input
# A cell may hold a plain number ("45.5") OR a formula ("45*25.4"), so units
# can be converted inline (inches -> mm, lb -> kg, ...). When a formula is
# entered it is evaluated and the *result* is written back into the cell, so
# the table shows 1143 (not "45*25.4"). Safe by design: arithmetic only,
# never eval()/exec().
# ----------------------------------------------------
_ALLOWED_OPERATORS = {
    ast.Add: _operator.add,
    ast.Sub: _operator.sub,
    ast.Mult: _operator.mul,
    ast.Div: _operator.truediv,
    ast.FloorDiv: _operator.floordiv,
    ast.Mod: _operator.mod,
    ast.Pow: _operator.pow,
    ast.USub: _operator.neg,
    ast.UAdd: _operator.pos,
}

_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def _eval_arithmetic(node):
    """Recursively evaluate an arithmetic-only AST node. Anything else raises."""
    if isinstance(node, ast.Constant):  # plain numbers
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPERATORS:
        return _ALLOWED_OPERATORS[type(node.op)](
            _eval_arithmetic(node.left), _eval_arithmetic(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPERATORS:
        return _ALLOWED_OPERATORS[type(node.op)](_eval_arithmetic(node.operand))
    raise ValueError("unsupported expression")


def _num_to_str(value):
    """1143.0 -> '1143', 45.5 -> '45.5', 54.43104 -> '54.43104'."""
    f = float(value)
    return str(int(f)) if f == int(f) else f"{round(f, 6):g}"


def evaluate_number(value, default=0.0):
    """
    Turn a cell value into a float (used for uploaded sheets).
      * numbers pass straight through
      * blank / None    -> default (0.0)
      * "45.5"          -> 45.5
      * "45*25.4"       -> 1143.0
    Raises ValueError on anything that isn't pure arithmetic.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return default if pd.isna(value) else float(value)
    if value is None:
        return default
    s = str(value).strip()
    if s == "" or s.lower() == "nan":
        return default
    if s.startswith("="):
        s = s[1:]
    s = s.replace("\u00d7", "*").replace("\u00b7", "*").replace(",", "")
    return float(_eval_arithmetic(ast.parse(s, mode="eval").body))


def _cell_to_display_and_value(raw):
    """
    Return (display_text, numeric_value) for one cell.
      * blank            -> ("", 0.0)
      * a plain number   -> kept exactly as typed, e.g. ("45.50", 45.5)
      * a valid formula  -> evaluated, e.g. ("1143", 1143.0)
      * anything invalid -> kept as text with value None (caller treats as 0)
    """
    if raw is None:
        return "", 0.0
    s = str(raw).strip()
    if s == "" or s.lower() == "nan":
        return "", 0.0
    s_norm = s[1:] if s.startswith("=") else s
    s_norm = s_norm.replace("\u00d7", "*").replace("\u00b7", "*").replace(",", "")
    if _NUMERIC_RE.match(s_norm):              # already a plain number -> leave as typed
        return s, float(s_norm)
    try:                                       # otherwise treat it as a formula
        val = float(_eval_arithmetic(ast.parse(s_norm, mode="eval").body))
        return _num_to_str(val), val
    except Exception:
        return s, None                         # invalid -> keep text, flag as bad


def _process_editor(edited):
    """
    From the raw editor dataframe build:
      display_df : what the cells should show (formulas replaced by results)
      numeric_df : clean numbers for the rest of the app
      invalid    : list of human-readable bad-entry messages
    """
    display_df = edited.copy()
    numeric_df = edited.copy()
    invalid = []

    # A row the user has just started typing into is created fresh by the data
    # editor, so its dropdown cells come back blank. Fill the OPTIONAL ones with
    # their defaults (Stackability = Auto, Loading Access = 4-Way) so the table
    # visibly shows what the engine will actually use. Packaging Material is
    # deliberately NOT defaulted — it is mandatory and must be chosen.
    for _col in ("Stackability", "Loading Access"):
        if _col in display_df.columns:
            def _is_blank(series):
                # NOTE: .astype(str) leaves a real NaN as NaN (not "nan"), so
                # fillna("") FIRST or empty rows are mistaken for filled ones.
                s = series.fillna("").astype(str).str.strip().str.lower()
                return s.isin(["", "nan", "none"])

            _blank = _is_blank(display_df[_col])
            if "Rack / Finished Good" in display_df.columns:
                _named = ~_is_blank(display_df["Rack / Finished Good"])
                _fill = _blank & _named
            else:
                _fill = _blank
            display_df.loc[_fill, _col] = _PKG_DEFAULTS[_col]
            numeric_df.loc[_fill, _col] = _PKG_DEFAULTS[_col]

    for col in _NUMERIC_TEXT_COLS:
        if col not in edited.columns:
            continue
        disp_vals, num_vals = [], []
        for idx, raw in edited[col].items():
            disp, val = _cell_to_display_and_value(raw)
            if val is None:
                invalid.append(f"row {idx + 1} \u2192 {col}: '{raw}'")
                val = 0.0
            disp_vals.append(disp)
            num_vals.append(val)
        display_df[col] = disp_vals
        numeric_df[col] = num_vals

    if "Rack / Finished Good" in edited.columns:
        names = edited["Rack / Finished Good"].fillna("").astype(str)
        display_df["Rack / Finished Good"] = names
        numeric_df["Rack / Finished Good"] = names

    if "Quantity" in edited.columns:
        qty = []
        for raw in edited["Quantity"]:
            try:
                qty.append(int(round(float(raw))) if not pd.isna(raw) else 0)
            except Exception:
                qty.append(0)
        display_df["Quantity"] = qty
        numeric_df["Quantity"] = qty

    return display_df, numeric_df, invalid


def _display_changed(edited, display_df):
    """
    True if the editor's contents need re-rendering — either a formula cell was
    rewritten to its value, OR a blank optional dropdown was filled with its
    default (Stackability = Auto, Loading Access = 4-Way). Both must trigger a
    refresh, otherwise the filled-in defaults are used by the engine but never
    become visible in the table.
    """
    for col in list(_NUMERIC_TEXT_COLS) + ["Stackability", "Loading Access"]:
        if col not in edited.columns or col not in display_df.columns:
            continue
        a = edited[col].fillna("").astype(str).reset_index(drop=True)
        b = display_df[col].fillna("").astype(str).reset_index(drop=True)
        if not a.equals(b):
            return True
    return False


def _clean_numeric_columns(df):
    """Evaluate formulas/decimals in an uploaded sheet's numeric columns."""
    out = df.copy()
    bad = []
    for col in _NUMERIC_TEXT_COLS:
        if col not in out.columns:
            continue
        vals = []
        for idx, raw in out[col].items():
            try:
                vals.append(evaluate_number(raw))
            except Exception:
                vals.append(0.0)
                bad.append(f"row {idx + 1} \u2192 {col}: '{raw}'")
        out[col] = vals
    if "Quantity" in out.columns:
        out["Quantity"] = [
            int(round(evaluate_number(v, default=0))) if not pd.isna(v) else 0
            for v in out["Quantity"]
        ]
    if bad:
        st.warning(
            "These entries weren't valid numbers/formulas and were treated as 0:\n\n- "
            + "\n- ".join(bad)
        )
    return out


def _do_rerun():
    """Re-run the script (compatible across Streamlit versions)."""
    if hasattr(st, "rerun"):
        st.rerun()
    else:  # older Streamlit
        st.experimental_rerun()


# ----------------------------------------------------
# Upload OR manual input (MAX TIGHT)
# ----------------------------------------------------
def render_upload_section():
    st.markdown(
        """
        <div style="
            font-size:18px;
            font-weight:600;
            margin-top:-16px;
            margin-bottom:2px;
            line-height:1.2;
        ">
            Upload Rack Excel or Use Manual Input
        </div>
        """,
        unsafe_allow_html=True,
    )

    uploaded = st.file_uploader("", type=["xlsx"])
    if uploaded:
        df = pd.read_excel(uploaded)
        # Stackability is optional -> fill its default if the sheet lacks it.
        if "Stackability" not in df.columns:
            df["Stackability"] = _PKG_DEFAULTS["Stackability"]
        # Loading Access is optional -> default to 4-Way if the sheet lacks it.
        if "Loading Access" not in df.columns:
            df["Loading Access"] = _PKG_DEFAULTS["Loading Access"]
        # Packaging Material is MANDATORY -> do NOT invent a default. If the
        # sheet has no material column, add it blank and warn the user.
        if "Packaging Material" not in df.columns:
            df["Packaging Material"] = ""
            st.error("Your sheet has no **Packaging Material** column. It is "
                     "required for correct stacking/weight results \u2014 please "
                     "add it and re-upload.")
        else:
            _blank = df["Packaging Material"].astype(str).str.strip()
            _named = df["Rack / Finished Good"].astype(str).str.strip() if \
                "Rack / Finished Good" in df.columns else _blank
            miss = df[(_named != "") & _blank.isin(["", "nan", "None"])]
            if not miss.empty:
                st.error("Some rows are missing **Packaging Material** (required). "
                         "Please fill it in for every rack.")
        # allow formulas / decimals in the uploaded sheet too
        return _clean_numeric_columns(df[ALL_INPUT_COLUMNS].copy())

    st.markdown("<div style='margin-top:-6px'></div>", unsafe_allow_html=True)
    st.info("No file uploaded \u2014 enter rack details manually below \U0001F447")
    with st.expander("\u2139\ufe0f  Input tips  \u2014  formulas, folded height, "
                     "max units per container"):
        st.markdown(
            "- **Formulas allowed** in any dimension/weight cell (converts on Enter): "
            "`45*25.4` in\u2192mm \u2022 `3*304.8` ft\u2192mm \u2022 `12*10` cm\u2192mm \u2022 "
            "`120*0.453592` lb\u2192kg\n"
            "- **Folded height:** if a rack ships folded, enter its **folded** height.\n"
            "- **Packaging Material is required** for every rack (it sets the "
            "stacking / weight capacity).\n"
            "- **Loading Access:** 4-Way = can be rotated. 2-Way = loads one way "
            "only \u2014 package length along the container **length** or **width**.\n"
            "- **Max units per container:** enter one package Details "
            "( L,W,H, Weight,Material), leave Quantity blank, and click "
            "**Calculate Loading**."
        )

    # Source-of-truth dataframe fed to the editor. We keep it in session_state
    # so we can write evaluated results back into the cells. Rebuild it if it's
    # missing or predates the packaging columns.
    def _fresh_base():
        return pd.DataFrame(
            {
                "Rack / Finished Good": [""],
                "Quantity": [1],
                "Length (MM)": [""],
                "Width (MM)": [""],
                "Height (MM)": [""],
                "Weight (Kg)": [""],
                "Packaging Material": [_PKG_DEFAULTS["Packaging Material"]],
                "Stackability": [_PKG_DEFAULTS["Stackability"]],
                "Loading Access": [_PKG_DEFAULTS["Loading Access"]],
            }
        )

    if ("manual_base" not in st.session_state
            or not set(ALL_INPUT_COLUMNS).issubset(st.session_state.manual_base.columns)):
        st.session_state.manual_base = _fresh_base()
    if "manual_ver" not in st.session_state:
        st.session_state.manual_ver = 0

    _help = "Type a number or a formula, e.g. 45*25.4 (converts to mm on Enter)."
    edited = st.data_editor(
        st.session_state.manual_base,
        num_rows="dynamic",
        use_container_width=True,
        key=f"manual_rack_input_{st.session_state.manual_ver}",
        column_config={
            "Rack / Finished Good": st.column_config.TextColumn("Rack / Finished Good"),
            "Quantity": st.column_config.NumberColumn(
                "Quantity", min_value=0, step=1, format="%d",
                help=("How many of this rack to ship. Entering ONE package and "
                      "leaving Quantity blank/0 asks 'how many fit in one "
                      "container?' instead."),
            ),
            "Length (MM)": st.column_config.TextColumn("Length (MM)", help=_help),
            "Width (MM)": st.column_config.TextColumn("Width (MM)", help=_help),
            "Height (MM)": st.column_config.TextColumn("Height (MM)", help=_help),
            "Weight (Kg)": st.column_config.TextColumn(
                "Weight (Kg)", help="Type a number or a formula, e.g. 120*0.453592 for lb\u2192kg."
            ),
            "Packaging Material": st.column_config.SelectboxColumn(
                "Packaging Material", options=ALL_MATERIALS, required=True,
                help=("REQUIRED. Metal Rack / Plastic Box / Plastic Bin / "
                      "Plastic Pallet / Wooden Pallet+Corrugate Box / "
                      "Metal Pallet+Corrugate Box / Wooden Box / Plywood Box."),
            ),
            "Loading Access": st.column_config.SelectboxColumn(
                "Loading Access", options=LOADING_ACCESS_OPTIONS,
                help=("4-Way = forklift can enter from any side, so the rack "
                      "may be rotated (most packages).  2-Way = the rack must "
                      "be loaded one way only: its length runs along the "
                      "container LENGTH, or along the container WIDTH."),
            ),
            "Stackability": st.column_config.SelectboxColumn(
                "Stackability", options=STACKABILITY_OPTIONS,
                help=("Auto = stack as many as safely fit (default).  "
                      "Non Stackable = keep single.  "
                      "G+N = exactly Ground + N high (G+1 = 2 levels, G+2 = 3, ...)."),
            ),
        },
    )

    display_df, numeric_df, invalid = _process_editor(edited)

    if invalid:
        st.warning(
            "These entries weren't valid numbers/formulas and were treated as 0:\n\n- "
            + "\n- ".join(invalid)
        )

    # If a formula was entered, bake the evaluated values back into the table
    # and re-render so the cells show the converted numbers.
    if _display_changed(edited, display_df):
        st.session_state.manual_base = display_df.reset_index(drop=True)
        st.session_state.manual_ver += 1
        _do_rerun()

    # Packaging Material is MANDATORY: flag any rack row that left it blank.
    missing_mat = []
    for idx, row in numeric_df.iterrows():
        name = str(row.get("Rack / Finished Good", "")).strip()
        mat  = str(row.get("Packaging Material", "")).strip()
        if name and (not mat or mat.lower() in ("nan", "none")):
            missing_mat.append(f"row {idx + 1}" + (f" ({name})" if name else ""))
    if missing_mat:
        st.error(
            "**Packaging Material is required** (it decides the stacking / weight "
            "capacity). Please pick a material for:\n\n- " + "\n- ".join(missing_mat)
        )

    return numeric_df[ALL_INPUT_COLUMNS]


# ----------------------------------------------------
# Results + Export
# ----------------------------------------------------
def _build_loading_plan_excel(export_rows):
    export_df = pd.DataFrame(
        export_rows,
        columns=[
            "Container",
            "Rack / Finished Good",
            "Quantity",
            "Weight Used (Kg)",
            "Weight Utilization (%)",
            "Volume Utilization (%)",
            "Total Transportation Cost",
        ],
    )
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Loading Plan")
    output.seek(0)
    return output.getvalue()


def render_results(
    containers,
    data,
    container_type,
    origin_city,
    destination_city,
    container_cost_df,
    dry_van_cost_df,
):
    container_cfg = CONTAINERS[container_type]
    container_volume = container_cfg["L"] * container_cfg["W"] * container_cfg["H"]
    n_containers = len(containers)

    # ---------------- Cost (needed for the Excel too) ----------------
    total_cost = calculate_total_cost(
        n_containers, container_type, origin_city, destination_city,
        container_cost_df, dry_van_cost_df,
    )
    cost_str = f"${total_cost:,.2f}" if total_cost is not None else ""

    # Pre-compute per-container utilisation + the export rows for the Excel.
    per_container = []          # (weight, weight_util, volume_util)
    export_rows = []
    for i, cont in enumerate(containers, start=1):
        total_weight = 0.0
        total_volume = 0.0
        for rack, qty in cont.items():
            row = data[data["Rack / Finished Good"] == rack].iloc[0]
            total_weight += qty * row["Weight (Kg)"]
            total_volume += qty * row["Length (MM)"] * row["Width (MM)"] * row["Height (MM)"]
        weight_util = (total_weight / container_cfg["MAX_WT"]) * 100
        volume_util = (total_volume / container_volume) * 100
        per_container.append((total_weight, weight_util, volume_util))
        for rack, qty in cont.items():
            export_rows.append([
                i, rack, qty, round(total_weight, 0),
                round(weight_util, 2), round(volume_util, 2), cost_str,
            ])

    excel_bytes = _build_loading_plan_excel(export_rows)

    # ══════════════════════════════════════════════════════════════════════
    # SINGLE-PACKAGE CAPACITY ANALYSIS
    # When the user entered exactly ONE package, also answer the common
    # question "how many of these fit in one container?". It reuses the real
    # packer, so it respects stacking, nesting, weight and orientation rules.
    # ══════════════════════════════════════════════════════════════════════
    if len(data) == 1:
        from engine.packing import max_units_in_one_container
        row = data.iloc[0].to_dict()
        try:
            with st.spinner("Calculating how many fit in one container\u2026"):
                max_units, detail = max_units_in_one_container(row, container_cfg)
        except Exception:
            max_units, detail = 0, {"error": "Could not compute capacity."}
        with st.container(border=True):
            if max_units <= 0:
                st.error("\U0001F4E6 " + detail.get("error",
                                                    "This package does not fit."))
            else:
                st.markdown(
                    f"\U0001F4E6 &nbsp;**Max {max_units} units** of "
                    f"**{str(row.get('Rack / Finished Good','this package'))}** "
                    f"per **{container_type}** &nbsp;\u00b7&nbsp; "
                    f"{detail['stacks']} floor positions \u00d7 "
                    f"{detail['per_stack']} high &nbsp;\u00b7&nbsp; "
                    f"{detail['total_weight']:,.0f} kg "
                    f"({detail['weight_pct']:.0f}%) &nbsp;\u00b7&nbsp; "
                    f"volume {detail['volume_pct']:.0f}% &nbsp;\u00b7&nbsp; "
                    f"limited by {detail['limited_by']}",
                    unsafe_allow_html=True,
                )

    # ══════════════════════════════════════════════════════════════════════
    # SUMMARY BOX (top): containers required + downloads.
    # The Excel is built instantly; the PDF layout report is built ONLY when
    # the user asks for it (it is slow for many containers, and for large jobs
    # people usually just need the container count).
    # ══════════════════════════════════════════════════════════════════════
    with st.container(border=True):
        c1, c2, c3 = st.columns([1.4, 1.3, 1.3])
        with c1:
            st.metric("Containers required", n_containers)
        with c2:
            st.download_button(
                "[\u2193] Download Load Plan (Excel)",
                excel_bytes,
                "container_loading_plan.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        with c3:
            gen = st.button("\U0001F4C4 Layout Report (PDF)", use_container_width=True)
            if gen:
                with st.spinner("Generating layout report (PDF)\u2026 this can take a "
                                "while when there are many containers."):
                    from engine.container_report import (
                        generate_pdf, container_label_from_type,
                    )
                    dims = data.set_index("Rack / Finished Good").to_dict("index")
                    label = container_label_from_type(container_type)
                    try:
                        st.session_state["layout_pdf_bytes"] = generate_pdf(
                            containers, dims, container_cfg, container_label=label)
                    except Exception as e:
                        st.session_state["layout_pdf_bytes"] = None
                        st.error(f"PDF error: {e}")
            if st.session_state.get("layout_pdf_bytes"):
                st.download_button(
                    "[\u2193] Download PDF",
                    st.session_state["layout_pdf_bytes"],
                    "container_layout_report.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
        if total_cost is not None:
            # st.caption() renders small grey text that users miss, so the cost
            # gets its own highlighted John Deere green strip instead.
            st.markdown(
                f"""
                <div style="
                    background-color:#EAF3E7;
                    border-left:6px solid #367C2B;
                    border-radius:6px;
                    padding:9px 14px;
                    margin-top:6px;
                    font-size:1.02rem;
                    color:#1E4620;">
                  <span style="font-weight:600;">\U0001F69A Estimated transportation cost:</span>
                  <span style="font-weight:800; color:#367C2B; font-size:1.18rem;">
                    &nbsp;{cost_str}</span>
                  <span style="color:#4A6B4A;">
                    &nbsp;&nbsp;({origin_city} &rarr; {destination_city})</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                """
                <div style="
                    background-color:#FBF3E2;
                    border-left:6px solid #C8A24A;
                    border-radius:6px;
                    padding:9px 14px;
                    margin-top:6px;
                    font-size:0.97rem;
                    color:#6B5A22;">
                  \u26A0\uFE0F Cost data not available for the selected route.
                </div>
                """,
                unsafe_allow_html=True,
            )

    # ══════════════════════════════════════════════════════════════════════
    # DETAILS: container-wise loading plan + utilisation.
    # ══════════════════════════════════════════════════════════════════════
    st.subheader("\U0001F4E6 Container-wise Loading Plan")
    for i, cont in enumerate(containers, start=1):
        total_weight, weight_util, volume_util = per_container[i - 1]
        st.write(f"### \U0001F69B Container {i}")
        st.dataframe(
            pd.DataFrame(cont.items(), columns=["Rack / Finished Good", "Quantity"]),
            use_container_width=True,
        )
        st.markdown(
            f"**Weight Used:** {total_weight:.0f} KG ({weight_util:.2f}%)  \n"
            f"**Volume Utilization:** {volume_util:.2f}%"
        )

