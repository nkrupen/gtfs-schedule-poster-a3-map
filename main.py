import pandas as pd
import zipfile
import os
import io
import re
import urllib.parse
import warnings
import sys
from datetime import datetime, timedelta
import subprocess
import math

# --- MAP IMPORTS ---
try:
    import geopandas as gpd
    from shapely.geometry import Point, LineString, Polygon, box as shapely_box
    from shapely.affinity import translate, rotate
    import osmnx as ox
except ImportError:
    print("Installing map dependencies...")
    subprocess.check_call(["pip", "install", "osmnx", "geopandas", "shapely"])
    import geopandas as gpd
    from shapely.geometry import Point, LineString, Polygon, box as shapely_box
    from shapely.affinity import translate, rotate
    import osmnx as ox

warnings.filterwarnings("ignore")


class GTFSSchedulePoster:
    """
    Large-format schedule poster generator.
    """

    def __init__(self, gtfs_path, routes_gpkg_path=None, water_geojson_path=None, theme_color="#3069b3"):
        self.gtfs_path = self._find_file(gtfs_path)
        self.data = {}

        # Use passed files or fallbacks
        self.routes_gpkg_path = self._find_file(routes_gpkg_path) if routes_gpkg_path else self._first_existing_path(
            ["routes.gpkg", "reitit.gpkg", "/mnt/data/routes.gpkg", "/content/routes.gpkg"]
        )
        self.water_geojson_path = self._find_file(water_geojson_path) if water_geojson_path else self._first_existing_path(
            ["blue_areas.geojson", "/mnt/data/blue_areas_kotka_hamina_pyhtaa.geojson", "/content/blue_areas.geojson"]
        )

        self.config = {
            "color": theme_color,
            "page_w_mm": 800,
            "page_h_mm": 1131,
            "font_main": "Arial, sans-serif",
            "min_departures": 8,

            # --- MAP CONFIG ---
            "map_bg_color": "#F3F0EA",
            "building_color": "#E0D8D3",
            "water_color": "#B5D0D0",
            "green_color": "#CDEBC0",

            "street_fill": "#FFFFFF",
            "street_casing": "#C8C4C0",
            "street_width": 1.9,
            "street_casing_width": 3.1,

            "route_color": "#4A4A4A",
            "route_opacity": 0.85,
            "pin_color": "#E57373",

            "street_label_color": "#666666",
            "street_font_size": 13.0,

            # Stop label boxes
            "font_stop": "Arial, sans-serif",
            "font_pin": "Arial, sans-serif",

            # Map Padding
            "map_padding": 75,
            "stop_radius": 5.0,
            "box_padding": 8.0,
            "box_font_size": 16.0,

            # Olet tässä marker
            "you_are_here_font_size": 24.0,
            "you_are_here_sub_font_size": 18.0,
            "you_are_here_dy": 32.0,

            "map_view_h_meters": 450,
            # Increased padding to prevent cropping when aspect ratio changes
            "scale_edge_pad": 100,
            "osm_font_size": 10.0,
        }

        self._load_data()

    def _find_file(self, filename):
        """Smart path resolver for Colab and local execution."""
        if not filename: return filename
        paths = [
            filename,
            f"/content/{filename}",
            os.path.join("assets", filename),
            os.path.join("/content/assets", filename),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return filename

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
        print(f"Loading GTFS data from {self.gtfs_path}...")
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

                if not self.data["stops"].empty:
                    self.data["stops"]["stop_lat"] = pd.to_numeric(
                        self.data["stops"]["stop_lat"], errors="coerce"
                    )
                    self.data["stops"]["stop_lon"] = pd.to_numeric(
                        self.data["stops"]["stop_lon"], errors="coerce"
                    )

        except FileNotFoundError:
            print(f"Error: The file {self.gtfs_path} was not found.")
            self.data = {}

    # ----------------------------
    # MAP GENERATION METHODS
    # ----------------------------
    def _geom_to_svg_path(self, geom, transform_func):
        if geom is None or geom.is_empty:
            return ""

        def coords_to_path(coords):
            coords = list(coords)
            if len(coords) < 2:
                return ""
            pts = [transform_func(x, y) for x, y in coords]
            return "M " + " L ".join([f"{x:.1f},{y:.1f}" for x, y in pts])

        if geom.geom_type == "LineString":
            return coords_to_path(geom.coords)
        elif geom.geom_type == "Polygon":
            return coords_to_path(geom.exterior.coords) + " Z"
        elif geom.geom_type == "MultiPolygon":
            return " ".join([coords_to_path(p.exterior.coords) + " Z" for p in geom.geoms])
        elif geom.geom_type == "MultiLineString":
            return " ".join([coords_to_path(l.coords) for l in geom.geoms])
        return ""

    def _load_layer_robust(self, path, target_bbox_3067, target_crs):
        if not path or not os.path.exists(path):
            return gpd.GeoDataFrame(geometry=[], crs=target_crs)

        try:
            meta = gpd.read_file(path, rows=1)
            native_crs = meta.crs if meta is not None else None
            if native_crs is None:
                native_crs = target_crs

            bbox_poly_3067 = shapely_box(*target_bbox_3067)
            bbox_gdf = gpd.GeoDataFrame(geometry=[bbox_poly_3067], crs=target_crs).to_crs(native_crs)
            bbox_native = bbox_gdf.iloc[0].geometry.bounds

            layer = gpd.read_file(path, bbox=bbox_native)
            if layer.empty:
                return gpd.GeoDataFrame(geometry=[], crs=target_crs)

            if layer.crs is None:
                layer = layer.set_crs(native_crs)

            layer = layer.to_crs(target_crs)
            layer = layer.clip(shapely_box(*target_bbox_3067))
            return layer
        except Exception as e:
            print(f"Warning: Could not load layer {path}: {e}")
            return gpd.GeoDataFrame(geometry=[], crs=target_crs)

    def _check_overlap_shapely(self, poly, obstacles):
        if poly is None or poly.is_empty:
            return False
        for ob in obstacles:
            try:
                if ob is None or ob.is_empty:
                    continue
                if poly.intersects(ob) or poly.distance(ob) < 0.1:
                    return True
            except Exception:
                continue
        return False

    def _estimate_text_box_dims(self, lines, font_size, padding):
        if not lines:
            return (0, 0)
        max_len = max(len(str(x)) for x in lines)
        w = max_len * font_size * 0.52 + padding * 2
        h = len(lines) * font_size * 1.25 + padding * 2
        return (w, h)

    def _wrap_line_list(self, line_list, max_len=26):
        items = [str(x).strip() for x in (line_list or []) if str(x).strip()]
        if not items:
            return []
        items = sorted(
            set(items),
            key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"([0-9]+)", s)],
        )
        line = ""
        out = []
        for it in items:
            separator = ", " if line else ""
            nxt = (line + separator + it)
            if len(nxt) <= max_len:
                line = nxt
            else:
                if line:
                    out.append(line)
                line = it
                if len(out) >= 2:
                    break
        if line and len(out) < 2:
            out.append(line)
        return out

    def _find_matching_column(self, gdf, values):
        if gdf is None or gdf.empty or not values:
            return None
        vals = set(map(str, values))
        best = None
        best_hits = 0
        for col in gdf.columns:
            if col.lower() in ("geometry",):
                continue
            try:
                s = set(gdf[col].dropna().astype(str).unique())
                hits = len(s.intersection(vals))
                if hits > best_hits:
                    best_hits = hits
                    best = col
            except Exception:
                continue
        return best if best_hits > 0 else None

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

    def _get_active_trips_for_week(self, stop_ids, monday_dt, sunday_dt):
        st = self.data.get("stop_times", pd.DataFrame())
        trips = self.data.get("trips", pd.DataFrame())
        if st.empty or trips.empty:
            return pd.DataFrame()

        stop_ids = set(map(str, stop_ids))
        stop_visits = st[st["stop_id"].astype(str).isin(stop_ids)]
        if stop_visits.empty:
            return pd.DataFrame()

        if "service_id" not in trips.columns:
            return pd.DataFrame()

        valid_sids = set()
        schedule_map = {}
        for sid in trips["service_id"].dropna().unique():
            active_tuple = self._is_service_active_in_week(sid, monday_dt, sunday_dt)
            if any(active_tuple):
                valid_sids.add(sid)
                schedule_map[sid] = active_tuple

        relevant_trips = trips[trips["trip_id"].isin(stop_visits["trip_id"])]
        active_trips = relevant_trips[relevant_trips["service_id"].isin(valid_sids)].copy()
        active_trips["week_pattern"] = active_trips["service_id"].map(schedule_map)
        return active_trips

    def _get_weekly_departure_counts(self, stop_ids, target_date):
        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)
        st = self.data.get("stop_times", pd.DataFrame())
        if st.empty:
            return {str(s): 0 for s in stop_ids}

        active_trips = self._get_active_trips_for_week(stop_ids, monday, sunday)
        if active_trips.empty:
            return {str(s): 0 for s in stop_ids}

        active_trip_ids = set(active_trips["trip_id"].astype(str).unique())
        sub = st[
            (st["trip_id"].astype(str).isin(active_trip_ids))
            & (st["stop_id"].astype(str).isin(set(map(str, stop_ids))))
        ].copy()
        if sub.empty:
            return {str(s): 0 for s in stop_ids}

        counts = sub.groupby(sub["stop_id"].astype(str)).size().to_dict()
        out = {str(s): int(counts.get(str(s), 0)) for s in stop_ids}
        return out

    def _get_stop_metadata(self, stop_ids, target_date):
        stop_ids = list(map(str, stop_ids))
        stops_df = self.data.get("stops", pd.DataFrame())
        st = self.data.get("stop_times", pd.DataFrame())
        trips = self.data.get("trips", pd.DataFrame())
        routes = self.data.get("routes", pd.DataFrame())
        if stops_df.empty or st.empty or trips.empty or routes.empty:
            return {sid: {"name": sid, "code": "", "zone": "", "lines": []} for sid in stop_ids}

        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)

        active_trips = self._get_active_trips_for_week(stop_ids, monday, sunday)
        if active_trips.empty:
            active_trip_ids = set()
        else:
            active_trip_ids = set(active_trips["trip_id"].astype(str).unique())

        stop_visits = st[
            (st["stop_id"].astype(str).isin(stop_ids))
            & (st["trip_id"].astype(str).isin(active_trip_ids))
        ]
        if stop_visits.empty:
            stop_visits = st[st["stop_id"].astype(str).isin(stop_ids)].copy()

        stop_visits = stop_visits.merge(trips[["trip_id", "route_id"]], on="trip_id", how="left")
        stop_visits = stop_visits.merge(routes[["route_id", "route_short_name"]], on="route_id", how="left")

        line_map = {}
        for sid, grp in stop_visits.groupby(stop_visits["stop_id"].astype(str)):
            line_list = [x for x in grp["route_short_name"].dropna().astype(str).tolist() if x.strip()]
            line_map[sid] = sorted(
                set(line_list),
                key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"([0-9]+)", s)],
            )

        out = {}
        sub_stops = stops_df[stops_df["stop_id"].astype(str).isin(stop_ids)].copy()
        for sid in stop_ids:
            row = sub_stops[sub_stops["stop_id"].astype(str) == sid]
            if row.empty:
                out[sid] = {"name": sid, "code": "", "zone": "", "lines": line_map.get(sid, [])}
                continue
            r = row.iloc[0]
            name = str(r.get("stop_name", sid) or sid)
            code = str(r.get("stop_code", "") or "")
            zone = str(r.get("zone_id", "") or "")
            if not str(code).startswith("K"):
                for col in row.columns:
                    val = str(r.get(col, "") or "")
                    if val.startswith("K") and len(val) < 8:
                        code = val
                        break
            out[sid] = {"name": name, "code": code, "zone": zone, "lines": line_map.get(sid, [])}
        return out

    def _get_high_frequency_routes(self, target_date, visible_stop_ids):
        visible_stop_ids = list(map(str, visible_stop_ids))
        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)

        st = self.data.get("stop_times", pd.DataFrame())
        trips = self.data.get("trips", pd.DataFrame())
        routes = self.data.get("routes", pd.DataFrame())
        if st.empty or trips.empty or routes.empty:
            return []

        active_trips = self._get_active_trips_for_week(visible_stop_ids, monday, sunday)
        if active_trips.empty:
            return []

        active_trip_ids = set(active_trips["trip_id"].astype(str).unique())
        sub = st[
            (st["trip_id"].astype(str).isin(active_trip_ids))
            & (st["stop_id"].astype(str).isin(set(visible_stop_ids)))
        ].copy()
        if sub.empty:
            return []

        sub = sub.merge(trips[["trip_id", "route_id"]], on="trip_id", how="left")
        sub = sub.merge(routes[["route_id", "route_short_name"]], on="route_id", how="left")
        sub["route_short_name"] = sub["route_short_name"].astype(str)

        cnt = sub.groupby("route_short_name").size().to_dict()
        min_dep = int(self.config.get("min_departures", 8))
        high = [k for k, v in cnt.items() if k and k != "nan" and int(v) >= min_dep]
        return high

    # ----------------------------
    # MAIN MAP GENERATOR
    # ----------------------------
    def generate_map_svg(self, stop_id, width_px=1000, height_px=800, target_date=None):
        print(f"Generating A0-style map for Stop {stop_id}...")
        try:
            stops_df = self.data.get("stops", pd.DataFrame())
            if stops_df.empty:
                return ""

            row = stops_df[stops_df["stop_id"].astype(str) == str(stop_id)]
            if row.empty:
                return ""

            c_lat = float(row.iloc[0]["stop_lat"])
            c_lon = float(row.iloc[0]["stop_lon"])

            if target_date is None:
                target_date = datetime(2025, 12, 8)

            target_crs = "EPSG:3067"
            center_gdf = gpd.GeoDataFrame(geometry=[Point(c_lon, c_lat)], crs="EPSG:4326").to_crs(target_crs)
            cx, cy = center_gdf.iloc[0].geometry.x, center_gdf.iloc[0].geometry.y

            width_mm = float(width_px)
            height_mm = float(height_px)

            view_h_meters = float(self.config.get("map_view_h_meters", 450))
            aspect = width_mm / height_mm
            view_w_meters = view_h_meters * aspect

            min_x = cx - (view_w_meters / 2)
            max_x = cx + (view_w_meters / 2)
            min_y = cy - (view_h_meters / 2)
            max_y = cy + (view_h_meters / 2)

            target_bbox_3067 = (min_x, min_y, max_x, max_y)
            box_geom_3067 = shapely_box(*target_bbox_3067)

            fetch_radius = max(view_w_meters, view_h_meters) / 2 * 1.25

            streets = gpd.GeoDataFrame(geometry=[], crs=target_crs)
            buildings = gpd.GeoDataFrame(geometry=[], crs=target_crs)
            green = gpd.GeoDataFrame(geometry=[], crs=target_crs)
            water = gpd.GeoDataFrame(geometry=[], crs=target_crs)

            try:
                # Streets
                G = ox.graph_from_point((c_lat, c_lon), dist=fetch_radius, network_type="all")
                streets = ox.graph_to_gdfs(G, nodes=False, edges=True)
                if streets is not None and not streets.empty:
                    streets = streets.to_crs(target_crs).clip(box_geom_3067)

                # Water (Local)
                water = self._load_layer_robust(self.water_geojson_path, target_bbox_3067, target_crs)

                # Green (OSM)
                green_tags = {
                    "landuse": ["grass", "forest", "meadow", "recreation_ground", "village_green", "allotments"],
                    "leisure": ["park", "garden", "pitch"],
                    "natural": "wood",
                }
                try:
                    green = ox.features_from_point((c_lat, c_lon), tags=green_tags, dist=fetch_radius)
                    if green is not None and not green.empty:
                        green = green.to_crs(target_crs).clip(box_geom_3067)
                        green = green[green.geometry.type.isin(["Polygon", "MultiPolygon"])]
                except Exception:
                    green = gpd.GeoDataFrame(geometry=[], crs=target_crs)

                # Buildings (OSM)
                try:
                    buildings = ox.features_from_point((c_lat, c_lon), tags={"building": True}, dist=fetch_radius)
                    if buildings is not None and not buildings.empty:
                        buildings = buildings.to_crs(target_crs).clip(box_geom_3067)
                        buildings = buildings[buildings.geometry.type.isin(["Polygon", "MultiPolygon"])]
                except Exception:
                    buildings = gpd.GeoDataFrame(geometry=[], crs=target_crs)

            except Exception as e:
                print(f"Warning: Issue fetching map data: {e}")

            # Visible stops
            wgs_center = center_gdf.to_crs("EPSG:4326")
            wgs_bounds = wgs_center.buffer(0.006).total_bounds
            visible_stops_df = stops_df[
                (stops_df["stop_lat"].between(wgs_bounds[1], wgs_bounds[3]))
                & (stops_df["stop_lon"].between(wgs_bounds[0], wgs_bounds[2]))
            ].copy()
            if visible_stops_df.empty:
                visible_stops_df = row.copy()

            stops_gdf = (
                gpd.GeoDataFrame(
                    visible_stops_df,
                    geometry=gpd.points_from_xy(visible_stops_df.stop_lon, visible_stops_df.stop_lat),
                    crs="EPSG:4326",
                )
                .to_crs(target_crs)
                .clip(box_geom_3067)
            )

            visible_stop_ids = stops_gdf["stop_id"].astype(str).unique().tolist()
            stop_metadata = self._get_stop_metadata(visible_stop_ids, target_date)
            departure_counts = self._get_weekly_departure_counts(visible_stop_ids, target_date)

            # Route layer
            routes_gdf = self._load_layer_robust(self.routes_gpkg_path, target_bbox_3067, target_crs)
            if routes_gdf is not None and not routes_gdf.empty:
                high_freq_routes = self._get_high_frequency_routes(target_date, visible_stop_ids)
                match_col = self._find_matching_column(routes_gdf, high_freq_routes)
                if match_col:
                    routes_gdf = routes_gdf[routes_gdf[match_col].astype(str).isin(set(map(str, high_freq_routes)))]

            def project(x, y):
                px = (x - min_x) / (max_x - min_x) * width_mm
                py = (max_y - y) / (max_y - min_y) * height_mm
                return px, py

            # SVG layers
            bg_svg, map_labels_svg, stop_balls_svg, lines_and_boxes_svg, pin_svg = [], [], [], [], []
            bg_svg.append(
                f'<rect x="0" y="0" width="{width_mm}" height="{height_mm}" fill="{self.config["map_bg_color"]}"/>'
            )

            # Water
            if water is not None and not water.empty:
                for geom in water.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(f'<path d="{path}" fill="{self.config["water_color"]}" stroke="none"/>')

            # Green
            if green is not None and not green.empty:
                for geom in green.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(f'<path d="{path}" fill="{self.config["green_color"]}" stroke="none"/>')

            # Buildings
            if buildings is not None and not buildings.empty:
                for geom in buildings.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(f'<path d="{path}" fill="{self.config["building_color"]}" stroke="none"/>')

            # Streets clearer (casing + fill)
            if streets is not None and not streets.empty:
                for geom in streets.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(
                            f'<path d="{path}" fill="none" stroke="{self.config["street_casing"]}" '
                            f'stroke-width="{self.config["street_casing_width"]}" stroke-linecap="round" stroke-linejoin="round"/>'
                        )
                for geom in streets.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(
                            f'<path d="{path}" fill="none" stroke="{self.config["street_fill"]}" '
                            f'stroke-width="{self.config["street_width"]}" stroke-linecap="round" stroke-linejoin="round"/>'
                        )

            # Routes
            if routes_gdf is not None and not routes_gdf.empty:
                for geom in routes_gdf.geometry:
                    path = self._geom_to_svg_path(geom, project)
                    if path:
                        bg_svg.append(
                            f'<path d="{path}" fill="none" stroke="{self.config["route_color"]}" stroke-width="2.4" '
                            f'opacity="{self.config["route_opacity"]}" stroke-linecap="round" stroke-linejoin="round"/>'
                        )

            # Split center/other stops
            placed_boxes_obstacles, center_stop_geom, other_stops = [], None, []
            for _, r in stops_gdf.iterrows():
                if str(r["stop_id"]) == str(stop_id):
                    center_stop_geom = r
                else:
                    other_stops.append(r)

            map_center_x, map_center_y = width_mm / 2, height_mm / 2
            you_are_here_obstacle = None

            # Pin + "You are here"
            if center_stop_geom is not None:
                gx, gy = center_stop_geom.geometry.x, center_stop_geom.geometry.y
                sx, sy = project(gx, gy)

                pin_svg.append(f'<circle cx="{sx}" cy="{sy}" r="7.0" fill="{self.config["pin_color"]}" stroke="none"/>')
                pin_svg.append(f'<circle cx="{sx}" cy="{sy}" r="2.5" fill="white" stroke="none"/>')

                label_txt_1 = "Olet tässä"
                label_txt_2 = "You are here"

                yah_fs = float(self.config.get("you_are_here_font_size", 24.0))
                yah_sub_fs = float(self.config.get("you_are_here_sub_font_size", 18.0))
                yah_dy = float(self.config.get("you_are_here_dy", 32.0))

                ty1 = sy + yah_dy
                ty2 = ty1 + yah_sub_fs + 2 

                # Stroke (Outline)
                pin_svg.append(
                    f'<text x="{sx}" y="{ty1}" font-family="{self.config["font_pin"]}" font-size="{yah_fs}" text-anchor="middle" '
                    f'stroke="white" stroke-width="4" paint-order="stroke">{label_txt_1}</text>'
                )
                pin_svg.append(
                    f'<text x="{sx}" y="{ty2}" font-family="{self.config["font_pin"]}" font-size="{yah_sub_fs}" text-anchor="middle" '
                    f'stroke="white" stroke-width="4" paint-order="stroke"><i>{label_txt_2}</i></text>'
                )

                # Fill (Black text)
                pin_svg.append(
                    f'<text x="{sx}" y="{ty1}" font-family="{self.config["font_pin"]}" font-size="{yah_fs}" text-anchor="middle" '
                    f'fill="#000">{label_txt_1}</text>'
                )
                pin_svg.append(
                    f'<text x="{sx}" y="{ty2}" font-family="{self.config["font_pin"]}" font-size="{yah_sub_fs}" text-anchor="middle" '
                    f'fill="#000"><i>{label_txt_2}</i></text>'
                )

                yah_w = max(len(label_txt_1) * yah_fs * 0.62, len(label_txt_2) * yah_sub_fs * 0.62)
                yah_h = (ty2 - ty1) + yah_fs + yah_sub_fs
                you_are_here_obstacle = shapely_box(
                    sx - yah_w / 2 - 8,
                    ty1 - yah_fs - 5,
                    sx + yah_w / 2 + 8,
                    ty2 + 8,
                )
                placed_boxes_obstacles.append(you_are_here_obstacle)

            # Stop obstacles
            all_stops_polys = []
            for _, r in stops_gdf.iterrows():
                gx, gy = r.geometry.x, r.geometry.y
                sx, sy = project(gx, gy)
                all_stops_polys.append(Point(sx, sy).buffer(self.config["stop_radius"] + 2.5))

            # --- SCALE BAR + NORTH ARROW + OSM (Lower Left Placement) ---
            scale_k = 1.35
            extra_drop = 40.0

            meters_per_mm = view_w_meters / width_mm
            target_scale_m = 200
            scale_bar_len_mm = target_scale_m / meters_per_mm
            if scale_bar_len_mm > width_mm / 3:
                target_scale_m = 100
                scale_bar_len_mm = target_scale_m / meters_per_mm

            base_pad = float(self.config.get("scale_edge_pad", 100))
            osm_fs_base = float(self.config.get("osm_font_size", 10.0))
            osm_fs_credit = osm_fs_base * 1.50

            # Dimensions
            sb_h = 30.0 * scale_k
            sb_w = (scale_bar_len_mm + 32.0) * scale_k
            na_w = 26.0 * scale_k
            na_h = 36.0 * scale_k
            gap_x = 10.0
            gap_y = 8.0

            osm_text = "© OpenStreetMap contributors"
            osm_text_w = 220.0
            osm_h = (osm_fs_credit + 6.0)

            module_w = sb_w + gap_x + na_w
            module_h = max(sb_h, na_h) + gap_y + osm_h

            # FORCE LOWER LEFT (BL)
            final_ex = base_pad
            final_ey = height_mm - module_h - base_pad + extra_drop

            lower_limit = height_mm - module_h - (base_pad / 3)
            final_ex = max(base_pad, min(final_ex, width_mm - module_w - base_pad))
            final_ey = max(base_pad, min(final_ey, lower_limit))

            # Scale bar (left part)
            sb_group_x = final_ex
            sb_group_y = final_ey
            sb_svg = (
                f'<g transform="translate({sb_group_x}, {sb_group_y}) scale({scale_k})">'
                f'<line x1="0" y1="18" x2="{scale_bar_len_mm}" y2="18" stroke="#333" stroke-width="1.8" />'
                f'<line x1="0" y1="12" x2="0" y2="18" stroke="#333" stroke-width="1.8" />'
                f'<line x1="{scale_bar_len_mm}" y1="12" x2="{scale_bar_len_mm}" y2="18" stroke="#333" stroke-width="1.8" />'
                f'<text x="{scale_bar_len_mm/2}" y="9.5" font-family="Arial" font-size="{osm_fs_base}" '
                f'text-anchor="middle" fill="#000">{target_scale_m} m</text>'
                f"</g>"
            )

            # North arrow (right of scale bar)
            na_group_x = final_ex + (scale_bar_len_mm * scale_k) + 8.0
            na_group_y = final_ey + 2.0
            na_svg = (
                f'<g transform="translate({na_group_x}, {na_group_y}) scale({scale_k})">'
                f'<path d="M 7,22 L 7,0 L 3.5,9 M 7,0 L 10.5,9" stroke="#333" stroke-width="1.9" fill="none" />'
                f'<text x="7" y="30" font-family="Arial" font-size="{osm_fs_base}" text-anchor="middle" fill="#000">N</text>'
                f"</g>"
            )

            # OSM Credit (below)
            osm_x = final_ex
            osm_y = final_ey + max(sb_h, na_h) + gap_y + osm_fs_credit
            osm_credit = (
                f'<text x="{osm_x}" y="{osm_y}" font-family="Arial" font-size="{osm_fs_credit}" fill="#000">{osm_text}</text>'
            )

            placed_boxes_obstacles.append(shapely_box(final_ex, final_ey, final_ex + module_w, final_ey + module_h))

            def place_box_for_stop(sid, sx, sy, lines_full, lines_simple):
                bpad = float(self.config["box_padding"])
                bfs = float(self.config["box_font_size"])

                bw_full, bh_full = self._estimate_text_box_dims(lines_full, bfs, bpad)
                bw_simple, bh_simple = self._estimate_text_box_dims(lines_simple, bfs, bpad)

                vec_x, vec_y = (sx - map_center_x), (sy - map_center_y)
                angle_to_center = math.atan2(vec_y, vec_x)

                distances = [30, 45, 60, 90, 120, 150, 180, 210, 240]
                angles = [0, 0.45, -0.45, 0.9, -0.9, 1.35, -1.35, 1.8, -1.8, 2.25, -2.25]

                dep_count = departure_counts.get(str(sid), 0)
                is_important_and_covered = False
                if dep_count > 10 and you_are_here_obstacle:
                    if you_are_here_obstacle.contains(Point(sx, sy)) or you_are_here_obstacle.distance(Point(sx, sy)) < 6:
                        is_important_and_covered = True

                def try_place(lines, bw, bh):
                    mp = float(self.config["map_padding"])
                    safety_margin = 20.0

                    for dist in distances:
                        for ang_offset in angles:
                            rad = angle_to_center + ang_offset
                            pcx, pcy = sx + math.cos(rad) * dist, sy + math.sin(rad) * dist
                            tlx, tly = pcx - bw / 2, pcy - bh / 2

                            if tlx < mp + safety_margin or tlx + bw > width_mm - (mp + safety_margin):
                                continue
                            if tly < mp + safety_margin or tly + bh > height_mm - (mp + safety_margin):
                                continue

                            box_cx, box_cy = tlx + bw / 2, tly + bh / 2
                            cand_box_poly = shapely_box(tlx, tly, tlx + bw, tly + bh)

                            conn_line = LineString([(sx, sy), (box_cx, box_cy)])
                            line_poly = conn_line.buffer(1.25)

                            obstacles_for_box = placed_boxes_obstacles + all_stops_polys
                            if self._check_overlap_shapely(cand_box_poly, obstacles_for_box):
                                continue

                            other_stops_polys = [p for p in all_stops_polys if not p.contains(Point(sx, sy))]
                            obstacles_for_line = list(placed_boxes_obstacles) + other_stops_polys
                            if is_important_and_covered and you_are_here_obstacle:
                                if you_are_here_obstacle in obstacles_for_line:
                                    obstacles_for_line.remove(you_are_here_obstacle)

                            if self._check_overlap_shapely(line_poly, obstacles_for_line):
                                continue

                            return (tlx, tly, bw, bh, box_cx, box_cy)
                    return None

                placement = try_place(lines_full, bw_full, bh_full)
                final_lines = lines_full
                if not placement:
                    placement = try_place(lines_simple, bw_simple, bh_simple)
                    final_lines = lines_simple

                if not placement:
                    return None
                return placement, final_lines

            other_stops_sorted = sorted(
                other_stops,
                key=lambda r: departure_counts.get(str(r["stop_id"]), 0),
                reverse=True,
            )

            max_labeled_stops = 10
            labeled = 0

            for r in other_stops_sorted:
                if labeled >= max_labeled_stops:
                    break

                sid = str(r["stop_id"])
                gx, gy = r.geometry.x, r.geometry.y
                sx, sy = project(gx, gy)

                if not (0 <= sx <= width_mm and 0 <= sy <= height_mm):
                    continue

                meta = stop_metadata.get(sid, {})
                name = str(meta.get("name", sid))
                code = str(meta.get("code", "") or "")
                lines = meta.get("lines", []) or []

                wrapped_lines = self._wrap_line_list(lines, max_len=28)
                full_box_lines = [name]
                if code and code != "nan":
                    full_box_lines.append(code)
                full_box_lines.extend(wrapped_lines)

                simple_box_lines = [name]
                if wrapped_lines:
                    simple_box_lines.extend(wrapped_lines[:1])

                placed = place_box_for_stop(sid, sx, sy, full_box_lines, simple_box_lines)
                if not placed:
                    continue

                (bx, by, bw, bh, cx_box, cy_box), final_lines = placed

                stop_balls_svg.append(
                    f'<circle cx="{sx}" cy="{sy}" r="{self.config["stop_radius"]}" fill="white" stroke="#333" stroke-width="1.7"/>'
                )

                lines_and_boxes_svg.append(
                    f'<line x1="{sx}" y1="{sy}" x2="{cx_box}" y2="{cy_box}" stroke="black" stroke-width="1.15"/>'
                )

                lines_and_boxes_svg.append(
                    f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="7" ry="7" fill="white" stroke="#333" stroke-width="1.25"/>'
                )

                pad = float(self.config["box_padding"])
                fs = float(self.config["box_font_size"])
                y_cursor = by + pad + fs
                for i, txt in enumerate(final_lines):
                    safe_txt = (
                        str(txt).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    )
                    font_weight = "normal"
                    if i == 0:
                        font_weight = "bold"
                    elif i == 1 and str(txt) == str(code):
                        font_weight = "bold"

                    lines_and_boxes_svg.append(
                        f'<text x="{bx + pad}" y="{y_cursor:.1f}" font-family="{self.config["font_stop"]}" '
                        f'font-size="{fs}" font-weight="{font_weight}" fill="#000">{safe_txt}</text>'
                    )
                    y_cursor += fs * 1.25

                placed_boxes_obstacles.append(shapely_box(bx, by, bx + bw, by + bh))
                labeled += 1

            # Street name labels
            placed_text_obstacles = []
            static_obstacles = list(placed_boxes_obstacles) + list(all_stops_polys)

            processed_names = set()
            if streets is not None and not streets.empty and "name" in streets.columns:
                for _, srow in streets.iterrows():
                    try:
                        nm = srow.get("name")
                        if isinstance(nm, list):
                            nm = nm[0] if nm else None
                        if not isinstance(nm, str) or not nm.strip():
                            continue
                        name = nm.strip()
                        if name in processed_names:
                            continue

                        geom = srow.geometry
                        if geom is None or geom.is_empty:
                            continue

                        if hasattr(geom, "length") and geom.length <= 50:
                            continue

                        if geom.geom_type == "LineString":
                            coords = list(geom.coords)
                            if len(coords) >= 2:
                                p1, p2 = coords[0], coords[-1]
                                dx, dy = p2[0] - p1[0], p2[1] - p1[1]
                                angle = math.degrees(math.atan2(dy, dx))
                                if angle > 90:
                                    angle -= 180
                                elif angle < -90:
                                    angle += 180

                                mid = geom.interpolate(0.5, normalized=True)
                                mx, my = project(mid.x, mid.y)

                                s_fs = float(self.config["street_font_size"])
                                text_w = len(name) * s_fs * 0.52
                                text_h = s_fs * 1.05
                                text_poly = shapely_box(mx - text_w / 2, my - text_h / 2, mx + text_w / 2, my + text_h / 2)
                                rotated_poly = rotate(text_poly, angle, origin=(mx, my))

                                if 0 <= mx <= width_mm and 0 <= my <= height_mm:
                                    all_obs = static_obstacles + placed_text_obstacles
                                    if not self._check_overlap_shapely(rotated_poly, all_obs):
                                        map_labels_svg.append(
                                            f'<text x="{mx}" y="{my}" font-family="{self.config["font_main"]}" '
                                            f'font-size="{s_fs}" fill="{self.config["street_label_color"]}" text-anchor="middle" '
                                            f'transform="rotate({-angle}, {mx}, {my})">{name}</text>'
                                        )
                                        processed_names.add(name)
                                        placed_text_obstacles.append(rotated_poly)
                    except Exception:
                        continue

            svg = (
                f'<svg viewBox="0 0 {width_mm} {height_mm}" xmlns="http://www.w3.org/2000/svg" '
                f'preserveAspectRatio="xMidYMid meet" width="100%" height="100%">'
                f'<defs><clipPath id="map-clip"><rect x="0" y="0" width="{width_mm}" height="{height_mm}" /></clipPath></defs>'
                + "".join(bg_svg)
                + f'<g clip-path="url(#map-clip)">'
                + "".join(map_labels_svg)
                + sb_svg
                + na_svg
                + osm_credit
                + "".join(lines_and_boxes_svg)
                + "".join(stop_balls_svg)
                + "".join(pin_svg)
                + f"</g>"
                + "</svg>"
            )
            return svg

        except Exception as e:
            print(f"Map generation error: {e}")
            import traceback
            traceback.print_exc()
            return (
                f'<svg viewBox="0 0 {width_px} {height_px}" xmlns="http://www.w3.org/2000/svg">'
                f'<rect width="100%" height="100%" fill="#eee"/>'
                f'<text x="50%" y="50%" text-anchor="middle">Map Unavailable</text>'
                f"</svg>"
            )

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
                '<div class="sc-h">Tunti |&nbsp;&nbsp;<span class="en"><i>hour</i></span></div>'
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
        """
        Calculates layout parameters based on data density.
        Returns: (cols, font_size, line_height, right_col_width_percent, vertical_margin, header_font_size, alareuna_offset)
        """
        density_score = row_count + (item_count / 6.5)

        # Base defaults
        font = "2.1em"
        line_height = "1.05"
        right_w = 40
        v_margin = "5px"
        alareuna_offset = "0px"

        if density_score < 40:
            font = "3.8em"
            line_height = "1.3"
            right_w = None
            v_margin = "25px"
        elif density_score < 60:
            font = "2.9em"
            line_height = "1.2"
            right_w = None
            v_margin = "20px"
        elif density_score < 85:
            font = "2.4em"
            line_height = "1.15"
            right_w = 40
            v_margin = "15px"
        elif density_score < 120:
            font = "2.1em"
            line_height = "1.1"
            right_w = 40
            v_margin = "10px"
        elif density_score < 145:
            # Primary solution for excessive data: Small decrease of font size
            font = "1.9em"
            line_height = "1.05"
            right_w = 40
            v_margin = "6px"
        else:
            # Secondary solution: Move alareuna below the poster edge
            font = "1.9em"
            line_height = "1.05"
            right_w = 40
            v_margin = "6px"
            alareuna_offset = "150px"  # Pushes the alareuna outwards beyond hidden overflow

        try:
            f_val = float(font.replace("em", ""))
            if f_val > 3.0:
                header_font = "2.5em"
            else:
                header_font = font
        except:
            header_font = font

        return 1, font, line_height, right_w, v_margin, header_font, alareuna_offset

    # ----------------------------
    # POSTER GENERATION
    # ----------------------------
    def generate_poster(self, stop_id, date_label, output_file, school_week_start, holiday_week_start, city_name, download=True):
        try:
            stop_name, stop_code, stop_zone = self.get_stop_info(stop_id)
            stop_name = self._clean_stop_name(stop_name)
            display_code = stop_code if (stop_code and stop_code != "???") else stop_id

            sched_html_chunks, legend_html, total_rows_count, total_items_count = self.generate_schedule_html_data(
                stop_id, school_week_start, holiday_week_start
            )
            school_trips = self._get_active_trips_for_week_single_stop(
                stop_id, school_week_start, school_week_start + timedelta(days=6)
            )

            cols, font_size, line_height, right_col_w, v_margin, header_font_size, alareuna_offset = self._get_dynamic_layout_params(total_rows_count, total_items_count)
            print(f"Stop {stop_id}: Rows={total_rows_count}, Items={total_items_count} -> Font: {font_size}, Header: {header_font_size}")

            if right_col_w:
                weekend_w = 100 - right_col_w
                weekend_flex = f"0 0 {weekend_w}%"
                right_col_flex = f"0 0 {right_col_w}%"
            else:
                weekend_flex = "0 0 40%"
                right_col_flex = "1"

            line_data = self.generate_line_bar_data(school_trips)
            map_svg = self.generate_map_svg(stop_id, width_px=1000, height_px=800, target_date=school_week_start)

            bus_icon_raw = self._read_svg_candidates(
                [self._find_file("bus-icon.svg"), "/mnt/data/bus-icon.svg", "bus-icon.svg"]
            )
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
            weekend_html = build_sched_html("Sat", "Lauantai", "Saturday") + build_sched_html("Sun", "Sunnuntai", "Sunday")

            # --- DYNAMIC QR CODE ---
            city_subdomain = city_name.lower()
            city_prefix = city_name.capitalize()
            schedule_url = f"https://{city_subdomain}.digitransit.fi/pysakit/{city_prefix}:{stop_id}"
            
            encoded_url = urllib.parse.quote(schedule_url)
            qr_img_url = f"https://api.qrserver.com/v1/create-qr-code/?size=1000x1000&color=000000&bgcolor=FFFFFF&data={encoded_url}"

            logo_svg_inline = self._read_svg_candidates([self._find_file("logo.svg"), "/mnt/data/logo.svg", "logo.svg"])
            alareuna_svg_inline = self._read_svg_candidates([self._find_file("alareuna.svg"), "/mnt/data/alareuna.svg", "alareuna.svg"])

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

            # --- READ TEMPLATE AND REPLACE PLACEHOLDERS ---
            template_path = self._find_file(os.path.join("templates", "poster_template.html"))
            if not template_path or not os.path.exists(template_path):
                print(f"Error: Could not find poster_template.html. Searched for: {template_path}")
                return

            with open(template_path, 'r', encoding='utf-8') as f:
                html = f.read()

            replacements = {
                "{{ PAGE_W_MM }}": self.config["page_w_mm"],
                "{{ PAGE_H_MM }}": self.config["page_h_mm"],
                "{{ COLOR }}": self.config["color"],
                "{{ FONT_MAIN }}": self.config["font_main"],
                "{{ ALAREUNA_OFFSET }}": alareuna_offset,
                "{{ WEEKEND_FLEX }}": weekend_flex,
                "{{ RIGHT_COL_FLEX }}": right_col_flex,
                "{{ V_MARGIN }}": v_margin,
                "{{ FONT_SIZE }}": font_size,
                "{{ HEADER_FONT_SIZE }}": header_font_size,
                "{{ LINE_HEIGHT }}": line_height,
                "{{ STOP_NAME }}": stop_name,
                "{{ DATE_LABEL }}": date_label,
                "{{ STOP_ZONE }}": stop_zone,
                "{{ STOP_NUMBER_HTML }}": stop_number_html,
                "{{ LOGO_HTML }}": logo_html,
                "{{ LINE_BAR_HTML }}": line_bar_html,
                "{{ MONFRI_HTML }}": monfri_html,
                "{{ LEGEND_HTML }}": legend_html,
                "{{ WEEKEND_HTML }}": weekend_html,
                "{{ MAP_SVG }}": map_svg,
                "{{ ALAREUNA_SVG }}": alareuna_svg_inline,
                "{{ QR_IMG_URL }}": qr_img_url
            }

            for key, val in replacements.items():
                html = html.replace(key, str(val))

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(html)

            print(f"✅ Generated HTML Poster: {os.path.abspath(output_file)}")

            pdf_filename = output_file.replace(".html", ".pdf")
            self.print_pdf_in_colab(output_file, pdf_filename, download=download)

        except Exception as e:
            print(f"Error generating poster: {e}")
            import traceback
            traceback.print_exc()

    def print_pdf_in_colab(self, html_path, pdf_path, download=True):
        print("Converting HTML to PDF using Google Chrome...")
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
            subprocess.run(cmd, check=True)
            print(f"✅ Generated PDF Poster: {os.path.abspath(pdf_path)}")
            
            if download:
                try:
                    from google.colab import files
                    import IPython
                    
                    ipython = IPython.get_ipython()
                    if ipython is not None and getattr(ipython, 'kernel', None) is not None:
                        files.download(pdf_path)
                except ImportError:
                    pass
        except Exception as e:
            print(f"❌ PDF Conversion Failed: {e}")


if __name__ == "__main__":
    def find_file_main(filename):
        if not filename: return filename
        paths = [
            filename,
            f"/content/{filename}",
            os.path.join("assets", filename),
            os.path.join("/content/assets", filename),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return filename

    print("--- File Setup ---")
    gtfs_input = input("Enter GTFS zip filename (default: gtfs.zip): ").strip() or "gtfs.zip"
    routes_input = input("Enter Routes GPKG filename (default: routes.gpkg): ").strip() or "routes.gpkg"
    water_input = input("Enter Water GeoJSON filename (default: blue_areas.geojson): ").strip() or "blue_areas.geojson"

    gtfs_file = find_file_main(gtfs_input)
    routes_file = find_file_main(routes_input)
    water_file = find_file_main(water_input)

    if gtfs_file and os.path.exists(gtfs_file):
        print(f"Found GTFS file at: {gtfs_file}")
        
        # New prompt for background and icon color
        color_input = input("Enter theme hex color for background and bus icons (default: #3069b3): ").strip() or "#3069b3"
        
        gen = GTFSSchedulePoster(gtfs_file, routes_file, water_file, theme_color=color_input)
        
        print("\n--- Timetable Configuration ---")
        stop_ids_input = input("Enter stop numbers separated by comma (e.g., 155527,155528): ").strip()
        
        if stop_ids_input:
            date_label = input("Enter printed date label (default: 10.8.2025–31.5.2026): ").strip() or "10.8.2025–31.5.2026"
            school_date_input = input("Enter a normal school week start date (YYYY-MM-DD) [default: 2025-12-08]: ").strip() or "2025-12-08"
            holiday_date_input = input("Enter a holiday week start date (YYYY-MM-DD) [default: 2025-12-29]: ").strip() or "2025-12-29"
            city_input = input("Enter the city name for the QR code (default: Kotka): ").strip() or "Kotka"
            
            try:
                school_week_start = datetime.strptime(school_date_input, "%Y-%m-%d")
                holiday_week_start = datetime.strptime(holiday_date_input, "%Y-%m-%d")
            except ValueError:
                print("❌ Invalid date format. Please use YYYY-MM-DD.")
                sys.exit(1)
            
            stop_ids = [s.strip() for s in stop_ids_input.split(",") if s.strip()]
            generated_files = []
            
            print(f"\nStarting batch generation for {len(stop_ids)} stops...")

            for stop_id in stop_ids:
                output_html = f"{stop_id}.html"
                output_pdf = f"{stop_id}.pdf"
                
                # Generate poster
                gen.generate_poster(stop_id, date_label, output_html, school_week_start, holiday_week_start, city_input, download=False)
                
                if os.path.exists(output_pdf):
                    generated_files.append(output_pdf)
                    if os.path.exists(output_html):
                        os.remove(output_html)
                else:
                    print(f"⚠️ Failed to generate PDF for {stop_id}")

            # Create ZIP file
            if generated_files:
                zip_filename = "schedules.zip"
                print(f"\nZipping {len(generated_files)} PDF files...")
                with zipfile.ZipFile(zip_filename, 'w') as zipf:
                    for file in generated_files:
                        zipf.write(file)
                        print(f"  Added {file} to zip")
                
                print(f"✅ ZIP created: {zip_filename}")
                
                # Download the ZIP file
                try:
                    from google.colab import files
                    import IPython
                    
                    ipython = IPython.get_ipython()
                    if ipython is not None and getattr(ipython, 'kernel', None) is not None:
                        print(f"Triggering download for {zip_filename}...")
                        files.download(zip_filename)
                except ImportError:
                    print(f"Automatic download not available. File saved as {zip_filename} in your working directory.")
            else:
                print("No PDF files were generated.")
                
    else:
        print(f"GTFS zip '{gtfs_input}' not found. Please ensure it is uploaded or placed in the correct directory.")
