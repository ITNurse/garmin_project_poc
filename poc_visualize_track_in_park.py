#!/usr/bin/env python3
"""
poc_visualize_track_in_park.py
------------------------------
Proof-of-concept: reads a single .fit or .gpx activity file, plots its GPS
track against provincial park boundaries, and reports what percentage of
points fall inside the named park (with and without a buffer).

Supports any province — park boundaries can be supplied as:
  • GeoJSON   (.geojson / .json)
  • Shapefile  (.shp)
  • CSV with a WKT geometry column  (.csv)

The park name field is auto-detected from common column names, or you can
specify it explicitly with --name-field.

The metric CRS used for buffering is auto-selected from the data's centroid,
so the script works anywhere in Canada (or beyond).

Usage:
    python poc_visualize_track_in_park.py <track_file> <parks_file> [options]

Examples:
    python poc_visualize_track_in_park.py sample_activity.fit parks/on_provincial_parks.geojson
    python poc_visualize_track_in_park.py sample_activity.fit parks/on_provincial_parks.geojson --park "Frontenac"
    python poc_visualize_track_in_park.py sample_activity.fit parks/nb_provincial_parks.csv --park "Mactaquac" --buffer 100

Optional arguments:
    --park          Park name to compare against (partial match, case-insensitive).
                    If omitted, lists available parks and prompts you to choose.
    --name-field    Column name containing the park name.
                    Auto-detected if omitted (tries: NAME, PROTECTED_AREA_NAME_ENG,
                    COMMON_SHORT_NAME, park_name, FEATURE_NAME).
    --buffer        Buffer distance in metres            (default: 50)
    --threshold     % of points required inside          (default: 95.0)
    --output-dir    Folder to save the PNG               (default: outputs/)
    --no-show       Skip the interactive plot window (still saves PNG)
"""

import sys
import argparse
import csv
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional heavy imports — give a helpful message if missing
# ---------------------------------------------------------------------------
try:
    import geopandas as gpd
    from shapely.geometry import Point
except ImportError:
    sys.exit(
        "Error: geopandas and shapely are required.\n"
        "Install with:  pip install geopandas shapely"
    )

try:
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("Error: matplotlib is required.\n  pip install matplotlib")

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # GPX-only mode if fitparse not available

try:
    import contextily as ctx
except ImportError:
    ctx = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WGS84_CRS = "EPSG:4326"
csv.field_size_limit(10_000_000)

# Candidate column names to search for the park name, in priority order.
# Extend this list if you encounter other common schemas.
NAME_FIELD_CANDIDATES = [
    "NAME",
    "PROTECTED_AREA_NAME_ENG",
    "COMMON_SHORT_NAME",
    "park_name",
    "FEATURE_NAME",
    "ParkName",
    "name",
]


# =============================================================================
# UTILITY: Auto-detect best UTM EPSG from a centroid longitude
# =============================================================================

def utm_epsg_from_lon(lon: float) -> str:
    """
    Return the EPSG code for the UTM zone that contains the given longitude.
    Covers the northern hemisphere (EPSG:326xx); sufficient for Canada.

    UTM zones are 6° wide starting at -180°.
    Zone number = floor((lon + 180) / 6) + 1
    Northern hemisphere EPSG = 32600 + zone_number
    """
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone
    return f"EPSG:{epsg}"


# =============================================================================
# TRACK LOADERS
# =============================================================================

def load_fit(path: Path) -> list[dict]:
    """Parse a .fit file and return a list of {lat, lon} dicts."""
    if FitFile is None:
        sys.exit(
            "Error: fitparse is required to read .fit files.\n"
            "Install with:  pip install fitparse"
        )
    points = []
    ff = FitFile(str(path))
    for record in ff.get_messages("record"):
        fields = {f.name: f.value for f in record}
        lat = fields.get("position_lat")
        lon = fields.get("position_long")
        if lat is not None and lon is not None:
            # Garmin stores coordinates as semicircles — convert to decimal degrees
            points.append({
                "lat": lat * (180 / 2**31),
                "lon": lon * (180 / 2**31),
            })
    return points


def load_gpx(path: Path) -> list[dict]:
    """Parse a .gpx file and return a list of {lat, lon} dicts."""
    import xml.etree.ElementTree as ET
    tree = ET.parse(path)
    root = tree.getroot()

    # GPX namespace varies by producer — detect it dynamically
    ns_match = re.match(r'\{(.*?)\}', root.tag)
    ns = {"gpx": ns_match.group(1)} if ns_match else {}
    tag = lambda t: f"{'{' + ns['gpx'] + '}' if ns else ''}{t}"  # noqa: E731

    points = []
    for trkpt in root.iter(tag("trkpt")):
        try:
            points.append({
                "lat": float(trkpt.attrib["lat"]),
                "lon": float(trkpt.attrib["lon"]),
            })
        except (KeyError, ValueError):
            continue
    return points


def load_track(path: Path) -> gpd.GeoDataFrame:
    """Load a .fit or .gpx file and return a GeoDataFrame of points (WGS84)."""
    suffix = path.suffix.lower()
    if suffix == ".fit":
        pts = load_fit(path)
    elif suffix == ".gpx":
        pts = load_gpx(path)
    else:
        sys.exit(f"Error: unsupported file type '{suffix}'. Expected .fit or .gpx")

    if not pts:
        sys.exit(f"Error: no GPS points found in {path.name}")

    print(f"  Loaded {len(pts):,} GPS points from {path.name}")
    geometries = [Point(p["lon"], p["lat"]) for p in pts]
    return gpd.GeoDataFrame(pts, geometry=geometries, crs=WGS84_CRS)


# =============================================================================
# PARK LOADER  — supports GeoJSON, Shapefile, and WKT CSV
# =============================================================================

def detect_name_field(columns: list[str]) -> str:
    """
    Return the first column name that matches a known park-name field.
    Raises ValueError if none found.
    """
    col_set = set(columns)
    for candidate in NAME_FIELD_CANDIDATES:
        if candidate in col_set:
            return candidate
    raise ValueError(
        f"Could not auto-detect a park name column.\n"
        f"Available columns: {columns}\n"
        f"Use --name-field to specify the correct one."
    )


def load_parks_csv(path: Path, name_field: str | None) -> gpd.GeoDataFrame:
    """Load parks from a CSV that has a WKT geometry column ('the_geom')."""
    from shapely import wkt as shapely_wkt

    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []

        # Resolve name field
        resolved_name_field = name_field or detect_name_field(columns)

        for row in reader:
            name = row.get(resolved_name_field, "").strip()
            wkt  = row.get("the_geom", "").strip()
            if not name or not wkt:
                continue
            try:
                geom = shapely_wkt.loads(wkt)
                rows.append({"NAME": name, "geometry": geom})
            except Exception as e:
                print(f"  [warn] WKT parse failed for {name!r}: {e}")

    if not rows:
        sys.exit("Error: no park geometries could be loaded from the CSV.")

    return gpd.GeoDataFrame(rows, crs=WGS84_CRS)


def load_parks(path: Path, name_field: str | None) -> tuple[gpd.GeoDataFrame, str]:
    """
    Load park boundaries from GeoJSON, Shapefile, or WKT CSV.
    Returns (GeoDataFrame, resolved_name_field).
    The returned GeoDataFrame always has the park name in a column called 'NAME'.
    """
    suffix = path.suffix.lower()

    if suffix == ".csv":
        gdf = load_parks_csv(path, name_field)
        resolved = "NAME"   # load_parks_csv normalises to NAME
    elif suffix in (".geojson", ".json", ".shp"):
        gdf = gpd.read_file(str(path))
        if gdf.crs is None:
            # GeoJSON spec mandates WGS84; assume it if CRS is missing
            gdf = gdf.set_crs(WGS84_CRS)
        else:
            gdf = gdf.to_crs(WGS84_CRS)

        # Resolve name field
        resolved = name_field or detect_name_field(list(gdf.columns))

        # Normalise to a column called NAME so the rest of the script is uniform
        if resolved != "NAME":
            gdf = gdf.rename(columns={resolved: "NAME"})
        resolved = "NAME"
    else:
        sys.exit(
            f"Error: unsupported parks file type '{suffix}'.\n"
            f"Expected .geojson, .json, .shp, or .csv"
        )

    if not path.exists():
        sys.exit(f"Error: parks file not found:\n  {path}")

    print(f"  Loaded {len(gdf)} parks from {path.name}")
    return gdf, resolved


# =============================================================================
# PARK SELECTION
# =============================================================================

def select_park(gdf_parks: gpd.GeoDataFrame, park_arg: str | None) -> tuple[str, gpd.GeoDataFrame]:
    """
    Return (park_name, single-row GeoDataFrame) for the chosen park.
    Partial, case-insensitive match on the NAME column.
    """
    if park_arg:
        matches = gdf_parks[gdf_parks["NAME"].str.contains(park_arg, case=False, na=False)]
        if matches.empty:
            print(f"\nNo park found matching '{park_arg}'. Available parks:")
            for name in sorted(gdf_parks["NAME"].unique()):
                print(f"  {name}")
            sys.exit(1)
        if len(matches) > 1:
            print(f"Multiple parks match '{park_arg}':")
            for name in matches["NAME"].unique():
                print(f"  {name}")
            sys.exit("Please use a more specific --park value.")
        return matches["NAME"].iloc[0], matches

    # Interactive selection
    names = sorted(gdf_parks["NAME"].unique())
    print("\nAvailable parks:")
    for i, name in enumerate(names, 1):
        print(f"  {i:>3}. {name}")
    while True:
        raw = input("\nEnter park number or name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(names):
                park_name = names[idx]
                break
        else:
            candidates = [n for n in names if raw.lower() in n.lower()]
            if len(candidates) == 1:
                park_name = candidates[0]
                break
            elif len(candidates) > 1:
                print(f"  Ambiguous — matches: {candidates}")
                continue
        print("  Not recognised, try again.")

    return park_name, gdf_parks[gdf_parks["NAME"] == park_name]


# =============================================================================
# ANALYSIS
# =============================================================================

def analyse(gdf_points: gpd.GeoDataFrame,
            park_row: gpd.GeoDataFrame,
            park_name: str,
            buffer_m: float,
            threshold_pct: float) -> dict:
    """
    Compute inside/outside counts for the raw boundary and a buffered boundary.

    The metric CRS for buffering is auto-detected from the park centroid's
    longitude, so this works correctly for any province/region.
    """
    # Dissolve in case the park is represented as multiple rows
    if hasattr(park_row.geometry, "union_all"):
        park_union = park_row.geometry.union_all()
    else:
        park_union = park_row.geometry.unary_union

    park_gdf = gpd.GeoDataFrame({"NAME": [park_name]}, geometry=[park_union], crs=WGS84_CRS)

    # Auto-detect appropriate UTM zone from the park centroid
    centroid_lon = park_union.centroid.x
    metric_crs   = utm_epsg_from_lon(centroid_lon)
    print(f"  Using metric CRS: {metric_crs}  (auto-detected from centroid lon={centroid_lon:.2f}°)")

    # ---- Baseline: no buffer ----
    inside_mask  = gdf_points.geometry.within(park_union)
    pct_inside   = inside_mask.mean() * 100
    count_inside = int(inside_mask.sum())
    count_total  = len(gdf_points)
    print(f"  Points inside park (no buffer):        {count_inside:,}/{count_total:,}  ({pct_inside:.2f}%)")

    # ---- Buffered check in metric CRS ----
    park_m   = park_gdf.to_crs(metric_crs)
    points_m = gdf_points.to_crs(metric_crs)

    park_buffered_geom_m  = park_m.geometry.buffer(buffer_m).iloc[0]
    inside_buffered       = points_m.geometry.within(park_buffered_geom_m)
    pct_inside_buffered   = inside_buffered.mean() * 100
    count_inside_buffered = int(inside_buffered.sum())
    print(f"  Points inside park ({buffer_m:.0f}m buffer): {count_inside_buffered:,}/{count_total:,}  ({pct_inside_buffered:.2f}%)")

    confirmed = pct_inside_buffered >= threshold_pct
    print(f"  Confirmed within park (≥ {threshold_pct:.0f}% with buffer)? {confirmed}")

    # Convert buffered geometry back to WGS84 for plotting
    park_buffered_gdf = gpd.GeoDataFrame(
        {"NAME": [f"{park_name} + {buffer_m:.0f}m buffer"]},
        geometry=[park_buffered_geom_m],
        crs=metric_crs,
    ).to_crs(WGS84_CRS)

    return {
        "park_gdf":          park_gdf,
        "park_buffered_gdf": park_buffered_gdf,
        "gdf_inside":        gdf_points[inside_buffered.values].copy(),
        "gdf_outside":       gdf_points[~inside_buffered.values].copy(),
        "pct_inside":        pct_inside,
        "pct_inside_buf":    pct_inside_buffered,
        "count_total":       count_total,
        "confirmed":         confirmed,
    }


# =============================================================================
# PLOT
# =============================================================================

def plot(track_path: Path,
         park_name: str,
         buffer_m: float,
         results: dict,
         gdf_points: gpd.GeoDataFrame,
         output_dir: Path,
         show: bool) -> Path:
    """Render and optionally display the park-vs-track plot. Always saves a PNG."""

    fig, ax = plt.subplots(figsize=(10, 10))

    # Park boundaries
    results["park_gdf"].boundary.plot(
        ax=ax, linewidth=2.5, color="yellow", label="Park boundary"
    )

    # Assign legend label to whichever series appears first; suppress the other
    inside_label  = "Activity" if len(results["gdf_inside"]) > 0 else "_nolegend_"
    outside_label = "Activity" if len(results["gdf_inside"]) == 0 else "_nolegend_"

    if len(results["gdf_inside"]) > 0:
        results["gdf_inside"].plot(
            ax=ax, markersize=6, color="lime", alpha=0.8, label=inside_label
        )
    if len(results["gdf_outside"]) > 0:
        results["gdf_outside"].plot(
            ax=ax, markersize=6, color="red", alpha=0.8, label=outside_label
        )

    # Title and labels
    status = "✓ Confirmed" if results["confirmed"] else "✗ Not confirmed"
    ax.set_title(
        f"{park_name}  —  {track_path.stem}\n"
        f"No buffer: {results['pct_inside']:.1f}% inside  |  "
        f"{buffer_m:.0f}m buffer: {results['pct_inside_buf']:.1f}% inside  |  {status}",
        fontsize=11,
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="best", fontsize=9)

    # Add basemap
    if ctx is not None:
        ctx.add_basemap(ax, crs=WGS84_CRS, source=ctx.providers.OpenStreetMap.Mapnik, zoom="auto")

    # Zoom to combined extent of park + track so both are always visible
    import numpy as np
    all_bounds = np.array([results["park_gdf"].total_bounds, gdf_points.total_bounds])
    minx = all_bounds[:, 0].min()
    miny = all_bounds[:, 1].min()
    maxx = all_bounds[:, 2].max()
    maxy = all_bounds[:, 3].max()
    pad_x = (maxx - minx) * 0.10 if maxx > minx else 0.01
    pad_y = (maxy - miny) * 0.10 if maxy > miny else 0.01
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)

    plt.tight_layout()

    # Save
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_park = re.sub(r'[^\w\-]', '_', park_name)
    png_path  = output_dir / f"{track_path.stem}__{safe_park}.png"
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    print(f"\n  Plot saved: {png_path}")

    if show:
        plt.show()

    plt.close(fig)
    return png_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Visualise a .fit/.gpx GPS track against park boundaries.\n"
            "Works with any province — supply a GeoJSON, Shapefile, or WKT CSV."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("track",        type=Path,
                        help="Path to a .fit or .gpx activity file")
    parser.add_argument("parks",        type=Path,
                        help="Path to park boundaries (.geojson, .shp, or .csv)")
    parser.add_argument("--park",       type=str,  default=None,
                        help="Park name to match (partial, case-insensitive). Prompts if omitted.")
    parser.add_argument("--name-field", type=str,  default=None,
                        help="Column containing park names (auto-detected if omitted).")
    parser.add_argument("--buffer",     type=float, default=50.0,
                        help="Buffer distance in metres (default: 50)")
    parser.add_argument("--threshold",  type=float, default=95.0,
                        help="Min %% of points inside to confirm visit (default: 95)")
    parser.add_argument("--output-dir", type=Path,  default=Path("outputs"),
                        help="Folder to save the PNG (default: outputs/)")
    parser.add_argument("--no-show",    action="store_true",
                        help="Skip interactive plot window (still saves PNG)")
    args = parser.parse_args()

    if not args.track.exists():
        sys.exit(f"Error: track file not found: {args.track}")
    if not args.parks.exists():
        sys.exit(f"Error: parks file not found: {args.parks}")

    print(f"\nTrack : {args.track}")
    print(f"Parks : {args.parks}")
    print()

    print("Loading data...")
    gdf_points       = load_track(args.track)
    gdf_parks, _     = load_parks(args.parks, args.name_field)

    print("\nSelecting park...")
    park_name, park_row = select_park(gdf_parks, args.park)
    print(f"  Selected: {park_name}")

    print("\nAnalysing...")
    results = analyse(gdf_points, park_row, park_name, args.buffer, args.threshold)

    print("\nPlotting...")
    plot(
        track_path = args.track,
        park_name  = park_name,
        buffer_m   = args.buffer,
        results    = results,
        gdf_points = gdf_points,
        output_dir = args.output_dir,
        show       = not args.no_show,
    )


if __name__ == "__main__":
    main()