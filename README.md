# GTFS Schedule Poster Generator (A3-format posters with a map)

A Python tool that parses GTFS (General Transit Feed Specification) data to generate A3-format, printable PDF schedule posters for transit stops.

This tool automatically calculates active bus trips, handles school vs. holiday schedules, generates localized QR codes, and dynamically scales typography to ensure dense schedules fit perfectly on the page. It also dynamically draws context-aware street maps directly into the final layout.

---

## Features

- **Direct GTFS Parsing:** Reads directly from a standard `gtfs.zip` file (no database required).
- - **Dynamic Map Generation:** Uses `osmnx` and `geopandas` to automatically fetch OpenStreetMap data and draw a localized map around the specific bus stop, including routes, streets, buildings, and water features.
- **Dynamic Typography:** Automatically scales font sizes down for busy stops to prevent text overflow.
- **School vs. Holiday Logic:** Compares two representative weeks (school & holiday) to correctly classify departures.
- **Simple HTML Templating:** Clean separation between Python data logic and HTML/CSS layout using strict `{{ placeholder }}` replacement.
- **Automated PDF Conversion:** Uses headless Google Chrome to generate high-quality, print-ready PDFs.
- **Batch Processing:** Generate posters for multiple stop IDs in one run and automatically bundle them into a single `.zip` file.
- **QR Code Integration:** Automatically generates Digitransit-based stop links using the provided city/area name.
- **Selection of color palette:** Allows the user to select a HEX color for background and bus icons.

---

## Copyright and License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.

**Author & Primary Maintainer:** Nikolay Krupen. The development work and testing was done using the data created by the City of Kotka. A special thanks to Paula Mussalo and Pyry Tuttavainen for helping to finish the posters' design!

---

## Prerequisites

- Python 3.8+
- **Google Chrome / Chromium**  
  The script relies on the Chrome CLI (`google-chrome --headless`) to generate PDFs. Chrome **must** be installed and available in your system's PATH.

---

## Project Structure

Before running the script, ensure your repository is structured exactly like this:

```text
gtfs-schedule-poster/
├── main.py
├── requirements.txt
├── gtfs.zip                                      <-- Your GTFS data feed
├── routes.gpkg                                   <-- GeoPackage containing route line strings
├── blue_areas.geojson       <-- GeoJSON for custom water bodies (can be derived using blue_areas_geojson_loader in the project folder)
├─ templates/                   <-- Required folder for HTML templates
├    └── poster_template.html
└── assets/                                      <-- Required folder for graphics (or place in root)
    ├── logo.svg                                 <-- Your transit agency logo
    └── alareuna.svg                             <-- Bottom graphic/banner
```

> **Important:** `Water body and GTFS files are large and are not included in the repository. You must download the GTFS feed and water body layers for your target transit agency / area and place them in the root directory. Ask a project maintainer if you need help e.g. with obtaining a .geojson file for water bodies for your area (or run the blue_areas_geojson_loader.ipynb file in the project folder).
> If Colab is used, it is sufficient to place all assets (gtfs.zip, blue_areas.geojson, logo and a bottom banner) to the /content folder, so their file path would be e.g. /content/alareuna.svg.
---

# Installation (Local Environment)

Clone this repository:

```bash
git clone https://github.com/nkrupen/gtfs-schedule-poster-a3-map.git
cd gtfs-schedule-poster-a3-map
```

Install the required Python packages:

```bash
pip install -r requirements.txt
```

Ensure:

- `gtfs.zip` is present in the root directory.
- `assets/logo.svg` exists.
- `templates/poster_template.html` exists.

---

# Usage (Local Environment)

Run the script:

```bash
python main.py
```

The interactive prompt will ask for:

1. **GTFS File:** Name of your GTFS zip (default: `gtfs.zip`)
2. **Routes GPKG:** GeoPackage for drawing route lines (default: `routes.gpkg`)
3. **Water GeoJSON:** Custom water areas (default: `blue_areas.geojson`)
4. **HEX color code:** Theme hex color for background and bus icons (default: #3069b3)
5. **Stop Numbers:** Comma-separated stop IDs (e.g., `155766,123456`)
6. **Date Label:** Validity period printed on the poster (e.g., `10.8.2025–31.5.2026`)
7. **School Week Start:** A normal Monday during the school term (`YYYY-MM-DD`)
8. **Holiday Week Start:** A normal Monday during school holidays (`YYYY-MM-DD`)
9. **City Name:** Used for the Digitransit QR code URL (e.g., `Kotka`)

---

# Running in Google Colab

Google Colab requires additional setup because Chrome is not installed by default and Python must be executed with `!python`.

---

## Step 1 – Install Google Chrome in Colab

Run this in a **separate Colab cell** before executing the script:

```bash
# 1. Update apt
!apt-get update

# 2. Download Chrome
!wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb

# 3. Install (dependency warnings are normal)
!dpkg -i google-chrome-stable_current_amd64.deb

# 4. Fix missing dependencies
!apt-get -f install -y

# 5. Verify installation
!google-chrome --version
```

---

## Step 2 – (Optional) Reset Project Folder in Colab

If the repository becomes nested or corrupted:

```bash
# 1. Move out of the folder
%cd /content

# 2. Force delete existing folder
!rm -rf gtfs-schedule-poster-a3-map

# 3. Clone fresh
!git clone https://github.com/nkrupen/gtfs-schedule-poster-a3-map.git

# 4. Enter folder
%cd gtfs-schedule-poster-a3-map
```

---

## Step 3 – Clone the repository

```bash
!git clone https://github.com/nkrupen/gtfs-schedule-poster-a3-map.git
%cd gtfs-schedule-poster-a3-map

```

---

## Step 4 – Run the Script in Colab

⚠️ In Colab, you must use `!python`:

```bash
!python main.py
```

Do **not** use:

```bash
python main.py
```

The interactive prompts will work inside the Colab cell.

---

## Step 5 – Download Posters Manually (If Needed)

If the ZIP file does not download automatically:

```python
from google.colab import files
files.download('schedules.zip')
```

---

# Output

The script will:

1. Generate individual HTML files.
2. Convert them into PDF posters.
3. Store them in a `generated_posters/` directory.
4. Bundle them into:

```text
schedules.zip
```

Located in the project root.

---

## Troubleshooting

- **PDF Generation Fails / Chrome Errors:** Ensure Chrome is correctly installed. On some Linux distributions, the binary might be called `chromium-browser` instead of `google-chrome`. If necessary, modify the subprocess command in `main.py`.
- **Missing Spatial Libraries:** If `osmnx` or `geopandas` fails to install locally, it is highly recommended to use a Conda environment (`conda install -c conda-forge geopandas osmnx`), as compiling spatial C-libraries via `pip` on Windows can be tricky.
- **Missing Map Data:** If the script cannot find your GTFS or `.gpkg`/`.geojson` files, it will fall back to generating a poster with an empty map background. Double-check your file paths.
- ## `FileNotFoundError: templates/poster_template.html`

Ensure:

- The `templates` folder exists.
- `poster_template.html` is inside it.
- There is no duplicated nested repository folder.

---

## PDF Generation Fails

Ensure Chrome is correctly installed.

On some Linux systems, the binary may be:

- `google-chrome-stable`
- `chromium-browser`

If necessary, modify the Chrome command in `main.py`.

---

## Nested Repository Issue in Colab

If your path looks like:

```text
gtfs-schedule-poster-a3-mapless/gtfs-schedule-poster-a3-mapless/main.py
```

You cloned the repository inside itself.  
Use the reset steps above.

---

# Notes & Best Practices

- Always use representative Mondays for school and holiday comparison. Choose the weeks that do not have any public holidays.
- Ensure your GTFS feed is up to date and internally consistent, as well as covering the period with the chosen weeks.
- Large stops may significantly scale down typography automatically.
- The script assumes standard GTFS structure (`trips.txt`, `stop_times.txt`, `calendar.txt`, etc.) and is tailored to Finnish names of calendars (e.g. containing "KOUL" for school days and "LOMA" for school holidays).
- When modifying the HTML template, keep all required `{{ placeholder }}` tags intact.

---
