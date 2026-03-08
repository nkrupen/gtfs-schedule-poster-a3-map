import pandas as pd
import zipfile
import os
import io
import re
import urllib.parse
import warnings
from datetime import datetime, timedelta
import subprocess
import shutil

warnings.filterwarnings("ignore")


class GTFSSchedulePoster:
    """
    Large-format schedule poster generator.
    Supports batch processing and zipping of results.
    """

    def __init__(self, gtfs_path):
        self.gtfs_path = gtfs_path
        self.data = {}

        self.config = {
            "color": "#3069b3",
            "page_w_mm": 800,
            "page_h_mm": 1131,
            "font_main": "Arial, sans-serif",
            "font_stop": "Arial, sans-serif",
            "box_padding": 8.0,
            "box_font_size": 16.0,
        }

        self._load_data()

    def _first_existing_path(self, candidates):
        for p in candidates:
            try:
                if p and os.path.exists(p):
                    return p
            except Exception:
                pass
        return None

    # ----------------------------
    # DATA LOADING
    # ----------------------------
    def _load_data(self):
        print(f"Loading GTFS data from local file: {self.gtfs_path}...")
        try:
            with zipfile.ZipFile(self.gtfs_path, "r") as z:

                def load_csv(name):
                    if name in z.namelist():
                        with z.open(name) as f:
                            content = f.read()
                            try:
                                text = content.decode("utf-8-sig")
                            except Exception:
                                text = content.decode("latin1")
                            first_line = text.splitlines()[0] if text.splitlines() else ""
                            sep = ";" if first_line.count(";") > first_line.count(",") else ","
                            df = pd.read_csv(
                                io.StringIO(text),
                                sep=sep,
                                dtype=str,
                                quotechar='"',
                                skipinitialspace=True,
                            )
                            df.columns = (
                                df.columns.str.lower().str.strip().str.replace('"', "")
                            )
                            return df
                    return pd.DataFrame()

                self.data["stops"] = load_csv("stops.txt")
                self.data["stop_times"] = load_csv("stop_times.txt")
                self.data["trips"] = load_csv("trips.txt")
                self.data["routes"] = load_csv("routes.txt")
                self.data["calendar"] = load_csv("calendar.txt")
                self.data["calendar_dates"] = load_csv("calendar_dates.txt")
                self.data["agency"] = load_csv("agency.txt")

        except FileNotFoundError:
            print(f"Error: The file {self.gtfs_path} was not found.")
            self.data = {}

    # ----------------------------
    # METADATA & UTILS
    # ----------------------------
    def _is_service_active_in_week(self, service_id, monday_dt, sunday_dt):
        active_days = [False] * 7
        cal = self.data.get("calendar", pd.DataFrame())
        if not cal.empty and "service_id" in cal.columns:
            row = cal[cal["service_id"] == service_id]
            if not row.empty:
                r = row.iloc[0]
                try:
                    start_date = datetime.strptime(r["start_date"], "%Y%m%d")
                    end_date = datetime.strptime(r["end_date"], "%Y%m%d")
                    if not (end_date < monday_dt or start_date > sunday_dt):
                        days = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
                        for i, day_name in enumerate(days):
                            if r.get(day_name) == "1":
                                current_day_date = monday_dt + timedelta(days=i)
                                if start_date <= current_day_date <= end_date:
                                    active_days[i] = True
                except Exception:
                    pass

        cal_dates = self.data.get("calendar_dates", pd.DataFrame())
        if not cal_dates.empty and "service_id" in cal_dates.columns:
            dates = cal_dates[cal_dates["service_id"] == service_id]
            for _, d_row in dates.iterrows():
                try:
                    exc_date = datetime.strptime(d_row["date"], "%Y%m%d")
                    if monday_dt <= exc_date <= sunday_dt:
                        wd = exc_date.weekday()
                        if d_row.get("exception_type") == "1":
                            active_days[wd] = True
                        elif d_row.get("exception_type") == "2":
                            active_days[wd] = False
                except Exception:
                    pass
        return tuple(active_days)

    def _get_active_trips_for_week_single_stop(self, stop_id, start_dt, end_dt):
        st = self.data.get("stop_times", pd.DataFrame())
        trips = self.data.get("trips", pd.DataFrame())
        if st.empty or trips.empty:
            return pd.DataFrame()

        stop_visits = st[st["stop_id"] == str(stop_id)]
        if stop_visits.empty:
            return pd.DataFrame()

        if "service_id" not in trips.columns:
            return pd.DataFrame()

        valid_sids = set()
        schedule_map = {}
        for sid in trips["service_id"].unique():
            active_tuple = self._is_service_active_in_week(sid, start_dt, end_dt)
            if any(active_tuple):
                valid_sids.add(sid)
                schedule_map[sid] = active_tuple

        relevant_trips = trips[trips["trip_id"].isin(stop_visits["trip_id"])]
        active_trips = relevant_trips[relevant_trips["service_id"].isin(valid_sids)].copy()
        active_trips["week_pattern"] = active_trips["service_id"].map(schedule_map)
        return active_trips

    # ----------------------------
    # HELPERS
    # ----------------------------
    def get_stop_info(self, stop_id):
        stops = self.data.get("stops", pd.DataFrame())
        if stops.empty:
            return "Unknown", "???", "Unknown"

        row = stops[stops["stop_id"] == str(stop_id)]
        if row.empty:
            return "Unknown", "???", "Unknown"

        name = row.iloc[0].get("stop_name", "Unknown")
        code = row.iloc[0].get("stop_code", "")

        raw_zone = str(row.iloc[0].get("zone_id", ""))
        zone = raw_zone
        if raw_zone == "1":
            zone = "A"
        elif raw_zone == "2":
            zone = "B"

        if not str(code).startswith("K"):
            for col in row.columns:
                val = str(row.iloc[0][col])
                if val.startswith("K") and len(val) < 8:
                    code = val
                    break

        return name, code, zone

    def _clean_stop_name(self, name):
        name = re.sub(r"(?i)\bpäätepysäkki\b", "", str(name))
        return name.strip()

    def _clean_line_dest(self, dest: str) -> str:
        s = str(dest or "").strip()
        if not s:
            return s
        s = re.sub(r"\(\s*KANTASATAMA\s*\)", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"\bKANTASATAMA\b", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"\s{2,}", " ", s).strip(" -–—,/|")
        s = re.sub(r"\s{2,}", " ", s).strip()
        return s

    def _read_svg_candidates(self, candidates):
        for p in candidates:
            try:
                if p and os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as sf:
                        return sf.read()
            except Exception:
                pass
        return ""

    def _svg_force_current_color(self, svg_text: str) -> str:
        if not svg_text:
            return ""
        s = svg_text.strip()
        if "<svg" in s and "xmlns=" not in s:
            s = s.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)

        s = re.sub(r'fill="[^"]*"', 'fill="currentColor"', s, flags=re.IGNORECASE)
        s = re.sub(r"fill\s*:\s*[^;\"']+;", "fill: currentColor;", s, flags=re.IGNORECASE)

        if "class=" not in s.split(">")[0]:
            s = s.replace("<svg", '<svg class="bus-icon"', 1)
        return s

    def _join_natural(self, items, conj):
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return ", ".join(items[:-1]) + f" {conj} " + items[-1]

    # ----------------------------
    # SCHEDULE HELPERS
    # ----------------------------
    def generate_line_bar_data(self, active_trips):
        if active_trips.empty:
            return []

        merged = active_trips.merge(self.data["routes"], on="route_id", how="left")

        lines_data = []
        grouped = merged.groupby("route_short_name")
        for name, group in grouped:
            headsign = ""
            if "trip_headsign" in group.columns and not group["trip_headsign"].mode().empty:
                headsign = group["trip_headsign"].mode()[0]
            headsign = self._clean_line_dest(headsign)
            lines_data.append({"num": name, "dest": headsign})

        def n_sort(s):
            return [int(t) if t.isdigit() else t.lower() for t in re.split("([0-9]+)", str(s))]

        lines_data.sort(key=lambda x: n_sort(x["num"]))
        return lines_data

    def _combine_patterns(self, p1, p2):
        if p1 is None:
            return p2
        if p2 is None:
            return p1
        return tuple(a or b for a, b in zip(p1, p2))

    def generate_schedule_html_data(self, stop_id, school_week_start, holiday_week_start):
        school_end = school_week_start + timedelta(days=6)
        holiday_end = holiday_week_start + timedelta(days=6)

        trips_s = self._get_active_trips_for_week_single_stop(stop_id, school_week_start, school_end)
        trips_h = self._get_active_trips_for_week_single_stop(stop_id, holiday_week_start, holiday_end)

        st = self.data["stop_times"]
        visits = st[st["stop_id"] == str(stop_id)]

        def process_trips(trips_df, is_school):
            if trips_df.empty:
                return []
            merged = visits.merge(trips_df, on="trip_id").merge(self.data["routes"], on="route_id", how="left")

            def parse_time(t):
                try:
                    parts = str(t).split(":")
                    return int(parts[0]), int(parts[1])
                except Exception:
                    return 0, 0

            departures = []
            for _, row in merged.iterrows():
                h, m = parse_time(row.get("arrival_time"))
                pat = row.get("week_pattern")
                line = row.get("route_short_name", "")
                departures.append(
                    {
                        "sig": (h, m, line),
                        "pattern": pat,
                        "line": line,
                        "h": h,
                        "m": m,
                        "origin": "S" if is_school else "H",
                    }
                )
            return departures

        deps_s = process_trips(trips_s, True)
        deps_h = process_trips(trips_h, False)

        merged_map = {}
        for d in deps_s:
            k = d["sig"]
            if k not in merged_map:
                merged_map[k] = {"S": None, "H": None, "line": d["line"], "h": d["h"], "m": d["m"]}
            merged_map[k]["S"] = self._combine_patterns(merged_map[k]["S"], d["pattern"])
        for d in deps_h:
            k = d["sig"]
            if k not in merged_map:
                merged_map[k] = {"S": None, "H": None, "line": d["line"], "h": d["h"], "m": d["m"]}
            merged_map[k]["H"] = self._combine_patterns(merged_map[k]["H"], d["pattern"])

        mon_fri_patterns = {}
        next_footnote = 1
        has_school_only_trips = False
        has_holiday_only_trips = False

        raw_rows = []
        for _, info in merged_map.items():
            pat_s, pat_h = info["S"], info["H"]
            final_type = "NORMAL"
            active_pat = None

            if pat_s and pat_h:
                final_type = "NORMAL"
                active_pat = pat_s
            elif pat_s and not pat_h:
                final_type = "SCHOOL"
                active_pat = pat_s
            elif (not pat_s) and pat_h:
                final_type = "HOLIDAY"
                active_pat = pat_h

            if final_type == "SCHOOL":
                has_school_only_trips = True
            if final_type == "HOLIDAY":
                has_holiday_only_trips = True

            if not active_pat:
                continue

            mf_slice = active_pat[0:5]
            if any(mf_slice):
                ft_idx = None
                if not all(mf_slice):
                    if mf_slice not in mon_fri_patterns:
                        mon_fri_patterns[mf_slice] = next_footnote
                        next_footnote += 1
                    ft_idx = mon_fri_patterns[mf_slice]
                raw_rows.append(
                    {
                        "bucket": "Mon-Fri",
                        "h": info["h"],
                        "m": info["m"],
                        "line": info["line"],
                        "footnote": ft_idx,
                        "type": final_type,
                    }
                )
            if active_pat[5]:
                raw_rows.append(
                    {
                        "bucket": "Sat", "h": info["h"], "m": info["m"], "line": info["line"], "footnote": None, "type": "NORMAL"
                    }
                )
            if active_pat[6]:
                raw_rows.append(
                    {
                        "bucket": "Sun", "h": info["h"], "m": info["m"], "line": info["line"], "footnote": None, "type": "NORMAL"
                    }
                )

        legend_html = '<div class="legend-container">'
        if mon_fri_patterns:
            days_fi = ["maanantaisin", "tiistaisin", "keskiviikkoisin", "torstaisin", "perjantaisin"]
            days_en = ["on Mondays", "on Tuesdays", "on Wednesdays", "on Thursdays", "on Fridays"]
            sorted_pats = sorted(mon_fri_patterns.items(), key=lambda x: x[1])
            for pat, fid in sorted_pats:
                idxs = [i for i, x in enumerate(pat) if x]
                fi_str = self._join_natural([days_fi[i] for i in idxs], "ja").capitalize()
                en_str = self._join_natural([days_en[i] for i in idxs], "and")
                legend_html += f'<div class="legend-item"><strong>{fid})</strong> {fi_str} / <span style="color:#000;"><i>{en_str}</i></span></div>'

        legend_html += '<div class="legend-note" style="text-align: left; margin-top: 8px; margin-bottom: 8px;">Arkipyhinä ajetaan sunnuntain vuorot. / <span class="en"><i>On public holidays, Sunday services are operated.</i></span></div>'

        legend_html += '<div class="legend-badges">'
        badge_base = "display:inline-block; padding:2px 6px; border-radius:4px; border:1px solid transparent; font-weight:bold; margin-right:6px;"

        if has_school_only_trips or has_holiday_only_trips:
            legend_html += (
                f'<div class="legend-item">Mustalla olevat vuorot ajetaan koulupäivinä sekä koulujen lomapäivinä / <span class="en"><i>Departures colored in black operated on school days and school holidays</i></span></div>'
            )

        if has_school_only_trips:
            style_school = badge_base + "background-color:#E3F2FD; border-color:#BBDEFB; color:#1565C0;"
            legend_html += (
                f'<div class="legend-item"><span style="{style_school}">&nbsp;</span> = '
                'Vain koulupäivinä / <span style="color:#000;"><i>On school days</i></span></div>'
            )
        if has_holiday_only_trips:
            style_holiday = badge_base + "background-color:#FFF3E0; border-color:#FFE0B2; color:#EF6C00;"
            legend_html += (
                f'<div class="legend-item"><span style="{style_holiday}">&nbsp;</span> = '
                'Vain koulujen lomapäivinä / <span style="color:#000;"><i>Only on school holidays</i></span></div>'
            )
        legend_html += "</div>"
        legend_html += "</div>"

        final_html_map = {}
        total_rows_count = 0
        total_items_count = 0

        for bucket in ["Mon-Fri", "Sat", "Sun"]:
            entries = [r for r in raw_rows if r["bucket"] == bucket]

            header_row = (
                '<div class="sc-row sc-header">'
                '<div class="sc-h">Tunti |&nbsp;<i>hour</i></div>'
                '<div class="sc-m">'
                'min | linja'
                '<span style="margin-left:2em; color:#000;"><i>min | route</i></span>'
                '</div>'
                '</div>'
            )

            if not entries:
                final_html_map[bucket] = header_row
                continue

            total_items_count += len(entries)
            entries.sort(key=lambda x: (x["h"], x["m"]))

            hours_map = {}
            for e in entries:
                note = f"<sup>{e['footnote']})</sup>" if e["footnote"] else ""
                base_style = "display:inline-block; width:4.5em; text-align:left; padding:1px 0; border-radius:4px; margin:0 2px; border:1px solid transparent;"

                if e["type"] == "SCHOOL":
                    style_str = base_style + "background-color:#E3F2FD; border-color:#BBDEFB; color:#1565C0;"
                    text_color = "#1565C0"
                elif e["type"] == "HOLIDAY":
                    style_str = base_style + "background-color:#FFF3E0; border-color:#FFE0B2; color:#EF6C00;"
                    text_color = "#EF6C00"
                else:
                    style_str = base_style + "color:#000000;"
                    text_color = "#000000"

                val = (
                    f"<div class='time-group' style='{style_str}'>"
                    f"<span style='color:{text_color}; font-weight:bold;'>{e['m']:02d}</span>{note}"
                    f"<span class='s-line' style='color:{text_color};'>/{e['line']}</span>"
                    f"</div>"
                )
                hours_map.setdefault(e["h"], []).append(val)

            srt_hours = sorted(hours_map.keys())
            html_chunk = header_row

            i = 0
            while i < len(srt_hours):
                ch = srt_hours[i]
                cm = "".join(hours_map[ch])
                eh, j = ch, i + 1
                while j < len(srt_hours):
                    nh = srt_hours[j]
                    nm = "".join(hours_map[nh])
                    if nh == eh + 1 and nm == cm:
                        eh = nh
                        j += 1
                    else:
                        break

                disp_ch = ch if ch < 24 else ch - 24
                disp_eh = eh if eh < 24 else eh - 24
                label = f"{disp_ch:02d}"
                if eh > ch:
                    label += f"&ndash;{disp_eh:02d}"

                html_chunk += f'<div class="sc-row"><div class="sc-h">{label}</div><div class="sc-m">{cm}</div></div>'
                total_rows_count += 1
                i = j

            final_html_map[bucket] = html_chunk

        return final_html_map, legend_html, total_rows_count, total_items_count

    def _get_dynamic_layout_params(self, row_count, item_count):
        density_score = row_count + (item_count / 6.0)

        # DEFAULT (PREFERRED) SETTINGS
        font = "3.8em"
        header_font = "2.5em"
        line_height = "1.3"
        v_margin = "25px"

        # Reduce logic
        if density_score > 55:
            font = "3.1em"
            line_height = "1.2"
            v_margin = "15px"

        if density_score > 80:
            font = "2.5em"
            header_font = "2.2em"
            line_height = "1.15"

        if density_score > 110:
            font = "2.1em"
            header_font = "2.0em"
            line_height = "1.1"
            v_margin = "5px"

        return 1, font, line_height, None, v_margin, header_font

    # ----------------------------
    # POSTER GENERATION
    # ----------------------------
    def generate_poster(self, stop_id, date_label, city, school_week_start, holiday_week_start, output_file, skip_pdf_conversion=False):
        try:
            stop_name, stop_code, stop_zone = self.get_stop_info(stop_id)
            if stop_name == "Unknown":
                print(f"⚠️ Warning: Stop ID {stop_id} not found in GTFS.")
                return None

            stop_name = self._clean_stop_name(stop_name)
            display_code = stop_code if (stop_code and stop_code != "???") else stop_id

            sched_html_chunks, legend_html, total_rows_count, total_items_count = self.generate_schedule_html_data(
                stop_id, school_week_start, holiday_week_start
            )
            school_trips = self._get_active_trips_for_week_single_stop(
                stop_id, school_week_start, school_week_start + timedelta(days=6)
            )

            cols, font_size, line_height, right_col_w, v_margin, header_font_size = self._get_dynamic_layout_params(total_rows_count, total_items_count)
            
            density_score = total_rows_count + (total_items_count / 6.0)
            print(f"Stop {stop_id}: Density {density_score:.1f} (Rows {total_rows_count}, Items {total_items_count})")
            
            line_data = self.generate_line_bar_data(school_trips)

            # FLIPPED PRIORITY + COLAB SMART SEARCH
            bus_icon_raw = self._read_svg_candidates([
                "bus-icon.svg", 
                "/content/bus-icon.svg", 
                "assets/bus-icon.svg"
            ])
            if not bus_icon_raw.strip():
                bus_icon_raw = """
                <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                  <path fill="currentColor" d="M4 16c0 1.1.9 2 2 2v1c0 .55.45 1 1 1s1-.45 1-1v-1h8v1c0 .55.45 1 1 1s1-.45 1-1v-1c1.1 0 2-.9 2-2V6c0-3-3.6-3-8-3S4 3 4 6v10zm3.5 1a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm9 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM6 6h12v6H6V6z"/>
                </svg>
                """.strip()

            bus_icon_svg = self._svg_force_current_color(bus_icon_raw)

            line_bar_items = []
            for item in line_data:
                line_bar_items.append(
                    f'<div class="lb-item"><span class="bus-icon-wrap">{bus_icon_svg}</span>'
                    f'<span class="lb-num">{item["num"]}</span>'
                    f'<span class="lb-dest">{item["dest"]}</span></div>'
                )
            line_bar_html = "".join(line_bar_items)

            def build_sched_html(key, fi, en):
                content = sched_html_chunks.get(key, "")
                if not content:
                    return ""
                return f'<div class="sc-block"><div class="sc-title">{fi} <span class="en"><i>{en}</i></span></div><div class="sc-content">{content}</div></div>'

            monfri_html = build_sched_html("Mon-Fri", "Maanantai–perjantai", "Monday–Friday")
            saturday_html = build_sched_html("Sat", "Lauantai", "Saturday")
            sunday_html = build_sched_html("Sun", "Sunnuntai", "Sunday")

            city_lower = city.lower()
            city_cap = city.capitalize()
            schedule_url = f"https://{city_lower}.digitransit.fi/pysakit/{city_cap}:{stop_id}"
            encoded_url = urllib.parse.quote(schedule_url)
            
            qr_img_url = f"https://api.qrserver.com/v1/create-qr-code/?size=1000x1000&color=000000&bgcolor=FFFFFF&margin=0&data={encoded_url}"

            # FLIPPED PRIORITY + COLAB SMART SEARCH
            logo_svg_inline = self._read_svg_candidates([
                "logo.svg", 
                "/content/logo.svg", 
                "assets/logo.svg"
            ])
            alareuna_svg_inline = self._read_svg_candidates([
                "alareuna.svg", 
                "/content/alareuna.svg", 
                "assets/alareuna.svg"
            ])

            logo_html = logo_svg_inline.strip()
            if not logo_html:
                logo_html = '<img src="https://jonnejaminne.fi/wp-content/uploads/2024/04/KSL_JM_bussit-logo_vaaka_rgb-1-1-1024x382.png" alt="Logo">'

            if not alareuna_svg_inline.strip():
                alareuna_svg_inline = (
                    '<svg viewBox="0 0 800 140" xmlns="http://www.w3.org/2000/svg">'
                    '<rect x="0" y="0" width="800" height="140" fill="#f0f0f0"/></svg>'
                )

            stop_number_html = ""
            if stop_zone != "B":
                stop_number_html = f"""
                <div class="h-info-group">
                    <div class="h-label">Pysäkkinumero <span class="en">| <i>Stop number</i></span></div>
                    <div class="h-value">{display_code}</div>
                </div>
                """

            html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    @page {{ size: {self.config["page_w_mm"]}mm {self.config["page_h_mm"]}mm; margin: 0; }}
    body {{
        margin: 0;
        padding: 0;
        font-family: {self.config["font_main"]};
        background-color: {self.config["color"]};
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
    }}

    .poster-container {{
        width: {self.config["page_w_mm"]}mm;
        height: {self.config["page_h_mm"]}mm;
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }}

    .en {{ color: #000; }}

    /* ---------- HEADER ---------- */
    .header {{
        flex: 0 0 auto;
        background-color: {self.config["color"]};
        padding: 15mm 20mm 5mm 20mm;
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        color: white;
    }}
    .header-left {{ flex: 1; min-width: 0; padding-left: 15mm; }}
    .h-stop-name {{
        font-size: 6.2em;
        font-weight: bold;
        color: white;
        line-height: 1;
        letter-spacing: -0.5px;
        overflow-wrap: anywhere;
    }}

    .h-valid-combined {{
        font-size: 3em;
        margin-top: 15px;
        font-weight: 300;
        color: white;
    }}
    .h-valid-combined .en {{ color: rgba(255,255,255,0.85); }}

    .header-right {{
        display: flex;
        align-items: center;
        gap: 20mm;
        text-align: center;
        flex: 0 0 auto;
    }}

    .header-info-wrapper {{
        display: flex;
        gap: 15mm;
        text-align: center;
    }}

    .h-info-group {{
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: flex-start;
    }}
    .h-label {{
        font-size: 2.2em;
        font-weight: normal;
        margin-bottom: 8px;
        opacity: 0.95;
        white-space: nowrap;
        color: white;
    }}
    .h-label .en {{ color: rgba(255,255,255,0.85); }}
    .h-value {{
        font-size: 5.6em;
        font-weight: bold;
        line-height: 0.95;
        color: white;
        white-space: nowrap;
    }}

    .header-logo {{
        display: block;
        height: 40mm;
        width: auto;
    }}
    .header-logo svg, .header-logo img {{
        height: 100%;
        width: auto;
        display: block;
    }}
    .header-logo svg {{ fill: white; }}

    /* ---------- LINE BAR ---------- */
    .line-bar-container {{
        flex: 0 0 auto;
        padding: 0 20mm;
        margin-bottom: 10mm;
    }}
    .line-bar {{
        background: white;
        padding: 12mm 15mm;
        display: flex;
        flex-wrap: wrap;
        gap: 30px;
        align-items: flex-start;
        border-radius: 30px;
    }}
    .lb-item {{
        display: flex;
        align-items: center;
        gap: 14px;
        margin-right: 40px;
        max-width: 100%;
    }}
    .bus-icon-wrap {{
        color: {self.config["color"]};
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        justify-content: center;
    }}
    .bus-icon {{ width: 50px; height: 50px; display: block; }}
    .lb-num {{
        font-size: 3em;
        font-weight: bold;
        margin-right: 0;
        flex: 0 0 auto;
    }}
    .lb-dest {{
        font-size: 2em;
        font-weight: 300;
        color: #000;
        text-transform: uppercase;
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
        line-height: 1.1;
        max-width: 760px;
    }}

    /* ---------- MAIN CONTENT LAYOUT ---------- */
    .content-wrap {{
        flex: 1 1 auto;
        display: flex;
        flex-direction: column;
        padding: 0 20mm 20mm 20mm;
        overflow: hidden;
    }}

    .unified-box {{
        background: white;
        border-radius: 30px;
        padding: 15mm;
        padding-bottom: 25mm; 
        display: flex;
        flex-direction: column;
        flex: 1;
    }}

    .monfri-box {{
        margin-bottom: 0;
        padding-bottom: 10mm;
        background: transparent;
    }}

    .bottom-row {{
        display: flex;
        gap: 20mm; 
        align-items: stretch;
        overflow: hidden;
        margin-top: 0;
        flex: 1;
    }}
    
    .weekend-box {{
        flex: 1;
        min-width: 0;
        background: transparent;
    }}

    .right-col {{
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 0;
        overflow: hidden;
        height: 100%;
        justify-content: flex-start;
    }}
    
    .spacer {{
        flex-grow: 1;
        min-height: 0;
    }}

    .alareuna-box {{
        flex: 0 0 auto;
        display: flex;
        flex-direction: column;
        justify-content: flex-end;
        overflow: hidden;
        padding: 0;
        background: transparent;
        margin-top: 20px;
    }}

    .alareuna-row {{
        display: block;
        width: 100%;
        margin: 0;
        padding: 0;
        position: relative;
    }}
    .alareuna-row svg {{
        display: block;
        width: 100%;
        height: auto;
    }}
    .qr-group {{
        position: absolute;
        bottom: 30px;
        right: 20px;
        z-index: 50;
    }}
    
    .qr-box {{
        width: 240px;
        height: 240px;
    }}
    
    .qr-img {{ width: 100%; height: 100%; display: block; }}

    /* ---------- SCHEDULE ---------- */
    .sc-block {{ margin-bottom: {v_margin}; break-inside: avoid; }}
    .sc-title {{
        font-size: {font_size};
        font-weight: bold;
        border-bottom: 4px solid black;
        padding-bottom: 8px;
        margin-bottom: {v_margin};
        break-after: avoid;
    }}
    .sc-title .en {{
        font-weight: normal;
        color: #000;
        font-size: 1em; /* Consistent size */
        margin-left: 10px;
    }}

    .sc-header {{
        border-bottom: 3px solid black;
        font-weight: bold;
        background-color: white !important;
        align-items: flex-end;
        padding-bottom: 6px;
        font-size: calc({header_font_size} * 1.15) !important;
    }}

    .sc-row {{
        display: flex;
        border-bottom: 1px solid #ddd;
        padding: 0;
        font-size: {font_size};
        line-height: {line_height};
        page-break-inside: avoid;
        break-inside: avoid;
        width: 100%;
        align-items: stretch;
    }}
    .sc-row:nth-child(odd):not(.sc-header) {{
        background-color: #f2f2f2;
    }}

    .sc-h {{
        width: 7.0em;
        font-weight: bold;
        white-space: nowrap;
        flex-shrink: 0;
        background-color: transparent;
        color: black;
        padding: 4px 6px;
        display: flex;
        align-items: center;
        justify-content: flex-start;
    }}
    .sc-m {{
        flex: 1;
        padding: 4px 0 4px 10px;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 0.8em;
        background-color: transparent;
    }}

    .time-group {{
        white-space: nowrap;
        display: inline-block;
    }}
    .s-line {{
        font-size: 1.0em;
        color: #000;
        vertical-align: middle;
        margin-right: 2px;
        margin-left: 2px;
    }}

    /* ---------- LEGEND ---------- */
    .legend-container {{
        margin-top: 25px;
        font-size: 2.1em;
        color: #333;
        line-height: 1.3;
        column-count: 2;
        column-gap: 20mm;
        width: 100%;
        break-inside: avoid;
    }}
    .legend-item {{ break-inside: avoid; margin-bottom: 5px; }}
    .legend-badges {{ margin-top: 10px; }}
    .legend-note {{ margin-top: 6px; }}

    .section-title {{
        font-size: 3.8em;
        font-weight: bold;
        margin-bottom: 15px;
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 20px;
    }}
    .section-title .en {{
        font-weight: normal;
        color: #000;
        font-size: 0.7em;
    }}
</style>
</head>
<body>
    <div class="poster-container">

        <div class="header">
            <div class="header-left">
                <div class="h-stop-name">{stop_name}</div>
                <div class="h-valid-combined">Aikataulut ovat voimassa | <span class="en"><i>Timetables valid</i></span> {date_label}</div>
            </div>

            <div class="header-right">
                <div class="header-info-wrapper">
                    <div class="h-info-group">
                        <div class="h-label">Vyöhyke <span class="en">| <i>Zone</i></span></div>
                        <div class="h-value">{stop_zone}</div>
                    </div>
                    {stop_number_html}
                </div>
                <div class="header-logo">
                    {logo_html}
                </div>
            </div>
        </div>

        <div class="line-bar-container">
            <div class="line-bar">{line_bar_html}</div>
        </div>

        <div class="content-wrap">
            <div class="unified-box">

                <div class="monfri-box">
                    <div class="section-title">
                        <div>
                            Pysäkkiaikataulu
                            <span class="en"><i>Stop timetable</i></span>
                        </div>
                        <div style="font-size: 0.7em; font-weight: normal; text-align: right;">
                            Ajat ovat arvioaikoja | <span class="en"><i>The times are estimates</i></span>
                        </div>
                    </div>
                    {monfri_html}
                    {legend_html}
                </div>

                <div class="bottom-row">
                    <div class="weekend-box">
                        {saturday_html}
                    </div>

                    <div class="right-col">
                        {sunday_html}
                        <div class="spacer"></div>
                        <div class="alareuna-box">
                            <div class="alareuna-row">
                                {alareuna_svg_inline}
                                <div class="qr-group">
                                    <div class="qr-box">
                                        <img class="qr-img" src="{qr_img_url}" alt="QR">
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

            </div>
        </div>

    </div>
</body>
</html>
"""

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(html)

            pdf_filename = output_file.replace(".html", ".pdf")
            
            if not skip_pdf_conversion:
                if self.print_pdf_in_colab(output_file, pdf_filename):
                    return pdf_filename
                else:
                    return None
            return output_file

        except Exception as e:
            print(f"Error generating poster for {stop_id}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def print_pdf_in_colab(self, html_path, pdf_path):
        """
        Converts HTML to PDF. Does NOT download automatically to avoid batch spam.
        """
        try:
            cmd = [
                "google-chrome",
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--print-to-pdf={pdf_path}",
                "--no-pdf-header-footer",
                "--virtual-time-budget=10000",
                html_path,
            ]
            # MUTE GOOGLE CHROME OUTPUT COMPLETELY
            subprocess.run(
                cmd, 
                check=True, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            return True
        except Exception as e:
            print(f"❌ PDF Conversion Failed for {html_path}: {e}")
            return False

    def generate_batch(self, input_string, date_label, city, school_week_start, holiday_week_start):
        """
        Takes comma-separated stop IDs, generates PDFs for all, and zips them.
        """
        stop_ids = [s.strip() for s in input_string.split(',') if s.strip()]
        if not stop_ids:
            print("No valid stop IDs found.")
            return

        output_dir = "generated_posters"
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir)
        os.makedirs(output_dir)

        generated_files = []

        print(f"Starting batch generation for {len(stop_ids)} stops...")
        
        for idx, stop_id in enumerate(stop_ids, 1):
            print(f"[{idx}/{len(stop_ids)}] Processing stop {stop_id}...")
            
            html_file = f"{stop_id}.html"
            result_pdf = self.generate_poster(stop_id, date_label, city, school_week_start, holiday_week_start, html_file)
            
            if result_pdf and os.path.exists(result_pdf):
                dest_path = os.path.join(output_dir, os.path.basename(result_pdf))
                shutil.move(result_pdf, dest_path)
                generated_files.append(dest_path)
                
                if os.path.exists(html_file):
                    os.remove(html_file)
            else:
                print(f"Failed to generate PDF for stop {stop_id}")

        if not generated_files:
            print("No posters were successfully generated.")
            return

        zip_filename = "schedule_posters"
        print(f"Zipping {len(generated_files)} files...")
        shutil.make_archive(zip_filename, 'zip', output_dir)
        final_zip_name = zip_filename + ".zip"
        
        print(f"✅ Batch complete! File saved as: {final_zip_name}")

        # Graceful Colab download handler
        try:
            from google.colab import files
            import IPython
            
            # Check if we are running in an interactive notebook cell (which has a kernel)
            ipython = IPython.get_ipython()
            if ipython is not None and getattr(ipython, 'kernel', None) is not None:
                print(f"Triggering download for {final_zip_name}...")
                files.download(final_zip_name)
            else:
                print("Interactive auto-download skipped (running in script mode). Please download the file manually from the folder menu.")
        except ImportError:
            # We are not in Colab at all
            print(f"Not running in Colab. Find '{final_zip_name}' in your working directory.")
        except Exception as e:
            # Catch-all to prevent any ugly traceback from breaking the script execution
            print(f"Auto-download skipped. Find '{final_zip_name}' in your working directory.")


if __name__ == "__main__":
    
    gtfs_input = input("Enter GTFS zip filename (default: gtfs.zip): ").strip() or "gtfs.zip"

    # SMART GTFS SEARCH: Check current dir, then /content/ (Colab default), then script folder
    possible_paths = [
        gtfs_input, 
        os.path.join("/content", gtfs_input),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), gtfs_input)
    ]
    
    gtfs_file = None
    for path in possible_paths:
        if os.path.exists(path):
            gtfs_file = path
            break

    if gtfs_file:
        print(f"Found GTFS file at: {gtfs_file}")
        gen = GTFSSchedulePoster(gtfs_file)
        
        user_input = input("Enter stop numbers separated by comma (e.g., 155527,155528): ").strip()
        
        if user_input:
            city_input = input("Enter city for URLs (default: Kotka): ").strip() or "Kotka"
            label_input = input("Enter date label (default: 10.8.2025–31.5.2026): ").strip() or "10.8.2025–31.5.2026"
            school_input = input("Enter School Week Monday (YYYY-MM-DD) [default 2025-12-08]: ").strip()
            holiday_input = input("Enter Holiday Week Monday (YYYY-MM-DD) [default 2025-12-29]: ").strip()
            
            school_dt = datetime.strptime(school_input, "%Y-%m-%d") if school_input else datetime(2025, 12, 8)
            holiday_dt = datetime.strptime(holiday_input, "%Y-%m-%d") if holiday_input else datetime(2025, 12, 29)
            
            gen.generate_batch(user_input, label_input, city_input, school_dt, holiday_dt)
        else:
            print("No input provided.")
    else:
        print(f"GTFS zip '{gtfs_input}' not found. Please ensure it is uploaded to Colab or placed in the directory.")
