#!/usr/bin/env python3
"""
poc_visualize_track_in_park.py
------------------------------
Proof-of-concept: reads a single .fit activity file, plots its GPS track
against provincial park boundaries, and reports what percentage of points
fall inside the named park (with and without a buffer).

Supports any province — park boundaries can be supplied as:
  • GeoJSON   (.geojson / .json)

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
# Optional heavy imports — give a helpful message if missing.
# We wrap each import in a try/except so that if a library isn't installed,
# we print a clear message telling the user how to fix it, rather than
# showing a confusing Python traceback.
# ---------------------------------------------------------------------------
try:
    import geopandas as gpd        # GeoDataFrame — like pandas but for geographic data
    from shapely.geometry import Point  # Represents a single lat/lon coordinate as a geometry object
except ImportError:
    sys.exit(
        "Error: geopandas and shapely are required.\n"
        "Install with:  pip install geopandas shapely"
    )

try:
    import matplotlib.pyplot as plt  # Used to draw the map and save it as a PNG
except ImportError:
    sys.exit("Error: matplotlib is required.\n  pip install matplotlib")

try:
    from fitparse import FitFile  # Reads binary .fit files produced by Garmin and other GPS devices
except ImportError:
    sys.exit("Error: fitparse is required.\n  pip install fitparse")

try:
    import contextily as ctx  # Adds a real map tile (e.g. OpenStreetMap) as a background to our plot
except ImportError:
    ctx = None  # contextily is optional — the plot will still work without a basemap

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# WGS84 is the standard GPS coordinate system used by most consumer devices.
# "EPSG:4326" is its official identifier used by geopandas and other GIS tools.
WGS84_CRS = "EPSG:4326"

# Different park boundary datasets use different column names for the park name.
# This list is checked in order — the first match found will be used.
# You can extend this list if you encounter a dataset with a different column name.
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
    Given a longitude value, return the EPSG code string for the appropriate
    UTM (Universal Transverse Mercator) projection zone.

    Why do we need this?
    GPS coordinates (latitude/longitude) are measured in degrees, not metres.
    To calculate a buffer of e.g. "50 metres", we need a coordinate system
    that uses actual distance units. UTM divides the world into 60 vertical
    zones, each 6 degrees wide, and uses metres as its unit — perfect for
    this kind of measurement.

    This function figures out which UTM zone covers the given longitude and
    returns the corresponding EPSG code. For Canada (northern hemisphere),
    these codes all start with EPSG:326xx.

    Example: lon = -66.5 (New Brunswick) → zone 20 → "EPSG:32620"
    """
    # Shift longitude from range [-180, 180] to [0, 360], then divide into 6° zones.
    # Adding 1 at the end because zones are numbered starting at 1, not 0.
    zone = int((lon + 180) / 6) + 1

    # Northern hemisphere UTM EPSG codes are 32600 + zone number.
    epsg = 32600 + zone

    return f"EPSG:{epsg}"


# =============================================================================
# TRACK LOADER — reads .fit files from GPS devices
# =============================================================================

def load_fit(path: Path) -> list[dict]:
    """
    Parse a Garmin .fit binary file and return a list of GPS points.

    Each point in the returned list is a dict with two keys:
        {"lat": <decimal degrees>, "lon": <decimal degrees>}

    .fit files store GPS coordinates in a unit called "semicircles" rather
    than decimal degrees. This function converts them automatically.

    """
    points = []

    # Open the .fit file. FitFile handles the binary decoding for us.
    ff = FitFile(str(path))

    # A .fit file contains many "message" types (heart rate, cadence, etc.).
    # We only want "record" messages, which contain GPS position data.
    for record in ff.get_messages("record"):

        # Each record has multiple fields. We convert them into a plain dict
        # so we can look up values by name, e.g. fields["position_lat"].
        fields = {f.name: f.value for f in record}

        lat = fields.get("position_lat")   # Returns None if this field is missing
        lon = fields.get("position_long")  # Note: Garmin uses "position_long", not "position_lon"

        # Skip any records that don't have both coordinates (e.g. heart rate-only records)
        if lat is not None and lon is not None:
            # Garmin stores angles in "semicircles": 2^31 semicircles = 180 degrees.
            # Multiply by (180 / 2^31) to convert to familiar decimal degrees.
            points.append({
                "lat": lat * (180 / 2**31),
                "lon": lon * (180 / 2**31),
            })

    return points


def load_track(path: Path) -> gpd.GeoDataFrame:
    """
    Load a .fit file and return its GPS points as a GeoDataFrame.

    A GeoDataFrame is like a regular spreadsheet (DataFrame) but each row
    also has an associated geographic shape — in this case, a Point.
    The CRS (coordinate reference system) is set to WGS84 (EPSG:4326),
    which is the standard GPS coordinate system.

    Returns a GeoDataFrame with columns: lat, lon, geometry.
    Exits with an error if the file type is unsupported or no points are found.
    """
    suffix = path.suffix.lower()  # e.g. ".fit"

    if suffix == ".fit":
        pts = load_fit(path)
    else:
        # Only .fit files are supported. Exit with a clear message if something else is provided.
        sys.exit(f"Error: unsupported file type '{suffix}'. Expected .fit")

    # If the file parsed successfully but contained no GPS data, something is wrong.
    if not pts:
        sys.exit(f"Error: no GPS points found in {path.name}")

    print(f"  Loaded {len(pts):,} GPS points from {path.name}")

    # Convert each {lat, lon} dict into a Shapely Point object.
    # Note: Point takes (longitude, latitude) — x before y — which is the GIS convention.
    geometries = [Point(p["lon"], p["lat"]) for p in pts]

    # Build the GeoDataFrame from our list of dicts, attach the geometry column,
    # and declare the coordinate system as WGS84.
    return gpd.GeoDataFrame(pts, geometry=geometries, crs=WGS84_CRS)


# =============================================================================
# PARK LOADER  — supports GeoJSON
# =============================================================================

def detect_name_field(columns: list[str]) -> str:
    """
    Given a list of column names from a park boundary dataset, find and return
    the one that contains the park names.

    Different data sources use different column names for the same concept.
    This function checks the column list against NAME_FIELD_CANDIDATES (defined
    near the top of the file) and returns the first match.

    Raises a ValueError with a helpful message if no known column is found,
    suggesting the user specify the column manually with --name-field.
    """
    # Convert the list to a set for fast membership testing (using 'in')
    col_set = set(columns)

    for candidate in NAME_FIELD_CANDIDATES:
        if candidate in col_set:
            return candidate  # Return the first match we find

    # If we get here, none of the known column names were present.
    raise ValueError(
        f"Could not auto-detect a park name column.\n"
        f"Available columns: {columns}\n"
        f"Use --name-field to specify the correct one."
    )


def load_parks_csv(path: Path, name_field: str | None) -> gpd.GeoDataFrame:
    """
    Load park boundary data from a CSV file that contains a WKT geometry column.

    WKT (Well-Known Text) is a standard text format for representing geographic
    shapes. For example, a polygon might look like:
        POLYGON((-66.5 45.9, -66.4 45.9, -66.4 45.8, -66.5 45.8, -66.5 45.9))

    This function expects the geometry to be in a column named 'the_geom',
    which is the convention used by many Canadian open-data portals.

    Returns a GeoDataFrame with columns: NAME (park name), geometry.
    """
    # shapely.wkt.loads() converts a WKT string into a Shapely geometry object
    from shapely import wkt as shapely_wkt

    rows = []  # Will hold one dict per successfully parsed park

    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)  # Reads each CSV row as a dict keyed by column header
        columns = reader.fieldnames or []

        # Figure out which column holds the park name.
        # Use the user-supplied name if provided, otherwise auto-detect.
        resolved_name_field = name_field or detect_name_field(columns)

        for row in reader:
            # Get the park name and geometry string, stripping any whitespace
            name = row.get(resolved_name_field, "").strip()
            wkt  = row.get("the_geom", "").strip()

            # Skip rows that are missing either the name or the geometry
            if not name or not wkt:
                continue

            try:
                # Parse the WKT string into a Shapely geometry object
                geom = shapely_wkt.loads(wkt)
                # Normalise the name column to "NAME" for consistency with other loaders
                rows.append({"NAME": name, "geometry": geom})
            except Exception as e:
                # Don't crash on bad rows — just warn and skip them
                print(f"  [warn] WKT parse failed for {name!r}: {e}")

    if not rows:
        sys.exit("Error: no park geometries could be loaded from the CSV.")

    return gpd.GeoDataFrame(rows, crs=WGS84_CRS)


def load_parks(path: Path, name_field: str | None) -> tuple[gpd.GeoDataFrame, str]:
    """
    Load park boundary data from a GeoJSON file.

    The returned GeoDataFrame will always have the park name stored in a column
    called 'NAME'. This normalisation means the rest of the script doesn't need
    to worry about which format the input was in.
    the input was in.

    Returns: (GeoDataFrame of all parks, the resolved name column string "NAME")
    """
    suffix = path.suffix.lower()  # e.g. ".geojson"

    if suffix == ".geojson":
        gdf = gpd.read_file(str(path))

        if gdf.crs is None:
            # The GeoJSON spec says coordinates should be WGS84, so if the CRS
            # is missing entirely, it's safe to assume WGS84 and set it.
            gdf = gdf.set_crs(WGS84_CRS)
        else:
            # If a different CRS is declared, reproject to WGS84 so everything
            # in this script uses the same coordinate system.
            gdf = gdf.to_crs(WGS84_CRS)

        # Determine which column holds the park names
        resolved = name_field or detect_name_field(list(gdf.columns))

        # Rename the column to "NAME" if it isn't already, so downstream code
        # can always refer to gdf["NAME"] regardless of the original column name.
        if resolved != "NAME":
            gdf = gdf.rename(columns={resolved: "NAME"})
        resolved = "NAME"

    else:
        sys.exit(
            f"Error: unsupported parks file type '{suffix}'.\n"
            f"Expected .geojson"
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
    Choose a single park from the loaded dataset, and return it along with its name.

    Two modes:
      1. If --park was provided on the command line, filter by that name
         (partial, case-insensitive match). Exits if zero or multiple parks match.
      2. If --park was not provided, print all park names and let the user
         pick interactively by typing a number or name.

    Returns: (park_name string, GeoDataFrame containing just that park's rows)
    """
    if park_arg:
        # Filter the full parks dataset to rows whose NAME contains park_arg.
        # case=False makes it case-insensitive; na=False skips any null values.
        matches = gdf_parks[gdf_parks["NAME"].str.contains(park_arg, case=False, na=False, regex=False)]

        if matches.empty:
            # No match found — print the full list so the user can see what's available
            print(f"\nNo park found matching '{park_arg}'. Available parks:")
            for name in sorted(gdf_parks["NAME"].unique()):
                print(f"  {name}")
            sys.exit(1)

        if len(matches) > 1:
            # Multiple parks matched the partial name — ask the user to be more specific
            print(f"Multiple parks match '{park_arg}':")
            for name in matches["NAME"].unique():
                print(f"  {name}")
            sys.exit("Please use a more specific --park value.")

        # Exactly one match — return its name and its row(s) as a GeoDataFrame
        return matches["NAME"].iloc[0], matches

    # --- Interactive selection (no --park argument given) ---

    # Get a sorted list of unique park names
    names = sorted(gdf_parks["NAME"].unique())

    # Print each name with a number so the user can pick by number
    print("\nAvailable parks:")
    for i, name in enumerate(names, 1):
        # :>3 right-aligns the number in a field of width 3, for tidy formatting
        print(f"  {i:>3}. {name}")

    # Keep prompting until the user gives a valid input
    while True:
        raw = input("\nEnter park number or name: ").strip()

        if raw.isdigit():
            # User entered a number — convert to a zero-based list index
            idx = int(raw) - 1
            if 0 <= idx < len(names):
                park_name = names[idx]
                break  # Valid selection, exit the loop
        else:
            # User entered text — do a partial case-insensitive search
            candidates = [n for n in names if raw.lower() in n.lower()]
            if len(candidates) == 1:
                park_name = candidates[0]
                break  # Exactly one match, exit the loop
            elif len(candidates) > 1:
                print(f"  Ambiguous — matches: {candidates}")
                continue  # Ask again

        print("  Not recognised, try again.")

    # Filter the full GeoDataFrame to only the rows for the chosen park
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
    Determine what fraction of GPS track points fall inside the park boundary.

    Two checks are performed:
      1. Strict boundary: points must be inside the exact park polygon.
      2. Buffered boundary: points must be within buffer_m metres of the boundary.
         This accounts for GPS inaccuracy — a device recording a trail right
         along the park edge might show some points just outside due to drift.

    The park is confirmed as visited if the buffered percentage meets or
    exceeds threshold_pct.

    Returns a dict containing:
      - park_gdf:          GeoDataFrame of the park boundary (WGS84)
      - park_buffered_gdf: GeoDataFrame of the buffered park boundary (WGS84)
      - gdf_inside:        track points inside the buffered boundary
      - gdf_outside:       track points outside the buffered boundary
      - pct_inside:        % of points inside the strict boundary
      - pct_inside_buf:    % of points inside the buffered boundary
      - count_total:       total number of track points
      - confirmed:         True if pct_inside_buf >= threshold_pct
    """
    # Some parks are stored as multiple rows (e.g. disconnected parcels).
    # union_all() / unary_union merges them into a single geometry for analysis.
    # We check for union_all first (newer shapely) and fall back to unary_union.
    if hasattr(park_row.geometry, "union_all"):
        park_union = park_row.geometry.union_all()
    else:
        park_union = park_row.geometry.unary_union

    # Wrap the merged geometry back into a one-row GeoDataFrame so we can
    # use geopandas methods on it (like .to_crs() and .buffer())
    park_gdf = gpd.GeoDataFrame({"NAME": [park_name]}, geometry=[park_union], crs=WGS84_CRS)

    # Choose the right UTM zone for metric calculations based on the park's location.
    # We use the longitude of the park's centroid (geographic centre).
    centroid_lon = park_union.centroid.x
    metric_crs   = utm_epsg_from_lon(centroid_lon)
    print(f"  Using metric CRS: {metric_crs}  (auto-detected from centroid lon={centroid_lon:.2f}°)")

    # ---- Check 1: Strict boundary (no buffer) ----
    # .within() returns a boolean Series — True for each point that lies inside park_union.
    inside_mask  = gdf_points.geometry.within(park_union)
    pct_inside   = inside_mask.mean() * 100   # mean() of booleans = fraction True → × 100 for %
    count_inside = int(inside_mask.sum())     # sum() counts the True values
    count_total  = len(gdf_points)
    print(f"  Points inside park (no buffer):        {count_inside:,}/{count_total:,}  ({pct_inside:.2f}%)")

    # ---- Check 2: Buffered boundary ----
    # Reproject both the park and the points to the metric CRS so that
    # buffer distances are in metres, not degrees.
    park_m   = park_gdf.to_crs(metric_crs)
    points_m = gdf_points.to_crs(metric_crs)

    # Expand the park polygon outward by buffer_m metres, then grab the resulting geometry.
    # .iloc[0] gets the first (and only) row's geometry.
    park_buffered_geom_m  = park_m.geometry.buffer(buffer_m).iloc[0]

    # Test which projected points fall within the buffered geometry
    inside_buffered       = points_m.geometry.within(park_buffered_geom_m)
    pct_inside_buffered   = inside_buffered.mean() * 100
    count_inside_buffered = int(inside_buffered.sum())
    print(f"  Points inside park ({buffer_m:.0f}m buffer): {count_inside_buffered:,}/{count_total:,}  ({pct_inside_buffered:.2f}%)")

    # The visit is "confirmed" if enough points are inside (with buffer)
    confirmed = pct_inside_buffered >= threshold_pct
    print(f"  Confirmed within park (≥ {threshold_pct:.0f}% with buffer)? {confirmed}")

    # Convert the buffered geometry back to WGS84 so it can be plotted on the same
    # map as the track points (which are in WGS84).
    park_buffered_gdf = gpd.GeoDataFrame(
        {"NAME": [f"{park_name} + {buffer_m:.0f}m buffer"]},
        geometry=[park_buffered_geom_m],
        crs=metric_crs,
    ).to_crs(WGS84_CRS)

    # Return everything the plot() function will need
    return {
        "park_gdf":          park_gdf,
        "park_buffered_gdf": park_buffered_gdf,
        # Use inside_buffered.values to get a plain numpy array for indexing the original gdf_points
        "gdf_inside":        gdf_points[inside_buffered.values].copy(),
        "gdf_outside":       gdf_points[~inside_buffered.values].copy(),  # ~ is "not" / bitwise invert
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
    """
    Draw a map showing the park boundary and the GPS track, colour-coded by
    whether each point is inside or outside the park (using the buffered boundary).

    The map is always saved as a PNG file. If show=True, it is also displayed
    in an interactive window.

    Returns the Path to the saved PNG file.
    """
    # Create a 10×10 inch figure with one set of axes to draw on.
    # fig is the overall figure; ax is the drawing area we'll add layers to.
    fig, ax = plt.subplots(figsize=(10, 10))

    # Draw just the outline (boundary) of the park polygon in yellow.
    # .boundary converts the filled polygon to its outline only.
    results["park_gdf"].boundary.plot(
        ax=ax, linewidth=2.5, color="yellow", label="Park boundary"
    )

    # Decide the legend label for the activity track.
    # matplotlib would normally show two "Activity" entries (one for inside, one for outside).
    # We want only one. The trick: assign the label "Activity" to whichever series is plotted
    # first, and use the special string "_nolegend_" to hide the second one.
    inside_label  = "Activity" if len(results["gdf_inside"]) > 0 else "_nolegend_"
    outside_label = "Activity" if len(results["gdf_inside"]) == 0 else "_nolegend_"

    # Plot inside points in green
    if len(results["gdf_inside"]) > 0:
        results["gdf_inside"].plot(
            ax=ax, markersize=6, color="lime", alpha=0.8, label=inside_label
        )

    # Plot outside points in red
    if len(results["gdf_outside"]) > 0:
        results["gdf_outside"].plot(
            ax=ax, markersize=6, color="red", alpha=0.8, label=outside_label
        )

    # Build the title string, including a confirmation status symbol
    status = "✓ Confirmed" if results["confirmed"] else "✗ Not confirmed"
    ax.set_title(
        f"{park_name}  —  {track_path.stem}\n"               # Park name and activity filename (no extension)
        f"No buffer: {results['pct_inside']:.1f}% inside  |  "
        f"{buffer_m:.0f}m buffer: {results['pct_inside_buf']:.1f}% inside  |  {status}",
        fontsize=11,
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="best", fontsize=9)  # "best" lets matplotlib pick the least-cluttered corner

    # Add an OpenStreetMap basemap tile underneath the plotted data, if contextily is available
    if ctx is not None:
        ctx.add_basemap(ax, crs=WGS84_CRS, source=ctx.providers.OpenStreetMap.Mapnik, zoom="auto")

    # Set the map extent so both the park and the full track are always visible,
    # with a 10% padding on each side so nothing is clipped at the edge.
    import numpy as np
    # Stack the bounding boxes of the park and the track into a 2-row array,
    # then take the min/max across both to get the combined extent.
    all_bounds = np.array([results["park_gdf"].total_bounds, gdf_points.total_bounds])
    minx = all_bounds[:, 0].min()  # leftmost longitude
    miny = all_bounds[:, 1].min()  # bottommost latitude
    maxx = all_bounds[:, 2].max()  # rightmost longitude
    maxy = all_bounds[:, 3].max()  # topmost latitude

    # Calculate 10% padding; fall back to a small fixed value if the extent is zero
    pad_x = (maxx - minx) * 0.10 if maxx > minx else 0.01
    pad_y = (maxy - miny) * 0.10 if maxy > miny else 0.01

    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)

    plt.tight_layout()  # Adjust layout so the title and labels don't get cut off

    # --- Save the plot as a PNG ---
    output_dir.mkdir(parents=True, exist_ok=True)  # Create the output folder if it doesn't exist

    # Replace any characters that aren't letters, digits, hyphens, or underscores
    # with underscores to make the filename safe across operating systems.
    safe_park = re.sub(r'[^\w\-]', '_', park_name)
    png_path  = output_dir / f"{track_path.stem}__{safe_park}.png"

    fig.savefig(png_path, dpi=150, bbox_inches="tight")  # dpi=150 gives a crisp but not huge image
    print(f"\n  Plot saved: {png_path}")

    # Optionally open an interactive window so the user can zoom and pan
    if show:
        plt.show()

    plt.close(fig)  # Free the memory used by this figure
    return png_path


# =============================================================================
# MAIN — entry point, argument parsing, and top-level orchestration
# =============================================================================

def main():
    """
    Parse command-line arguments, then run the full pipeline:
      1. Load the GPS track from a .fit file
      2. Load the park boundary dataset
      3. Select the target park (by name or interactively)
      4. Analyse how many track points are inside the park
      5. Plot the results and save a PNG
    """
    # argparse handles reading arguments from the command line and printing --help text.
    parser = argparse.ArgumentParser(
        description=(
            "Visualise a .fit GPS track against GeoJSON park boundaries."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Positional arguments (required, no -- prefix)
    parser.add_argument("track",        type=Path,
                        help="Path to a .fit activity file")
    parser.add_argument("parks",        type=Path,
                        help="Path to park boundaries (.geojson)")

    # Optional arguments (-- prefix, all have defaults)
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

    args = parser.parse_args()  # Parse sys.argv and populate the args namespace

    # Validate that both input files actually exist before doing any heavy work
    if not args.track.exists():
        sys.exit(f"Error: track file not found: {args.track}")
    if not args.parks.exists():
        sys.exit(f"Error: parks file not found: {args.parks}")

    print(f"\nTrack : {args.track}")
    print(f"Parks : {args.parks}")
    print()

    # Step 1 & 2: Load data
    print("Loading data...")
    gdf_points   = load_track(args.track)
    gdf_parks, _ = load_parks(args.parks, args.name_field)
    # The _ discards the second return value (the resolved name field string),
    # which we don't need after this point.

    # Step 3: Select the target park
    print("\nSelecting park...")
    park_name, park_row = select_park(gdf_parks, args.park)
    print(f"  Selected: {park_name}")

    # Step 4: Run the inside/outside analysis
    print("\nAnalysing...")
    results = analyse(gdf_points, park_row, park_name, args.buffer, args.threshold)

    # Step 5: Draw and save the map
    print("\nPlotting...")
    plot(
        track_path = args.track,
        park_name  = park_name,
        buffer_m   = args.buffer,
        results    = results,
        gdf_points = gdf_points,
        output_dir = args.output_dir,
        show       = not args.no_show,  # Flip the flag: --no-show stores True, but show=False
    )


# This block ensures main() is only called when the script is run directly
# (e.g. `python poc_visualize_track_in_park.py ...`), not when it's imported
# as a module by another script.
if __name__ == "__main__":
    main()