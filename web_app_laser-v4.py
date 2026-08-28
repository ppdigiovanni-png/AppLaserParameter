"""
Cotizador Láser PRO — Remeciendo Estudio & Taller Laser
=========================================================
Mejoras respecto de la versión original:
  1. Lectura REAL de geometría (DXF, SVG y trazado de contorno en PNG/JPG)
     en vez de valores fijos (650mm / 120x80mm) para todo lo que no fuera DXF.
  2. Motor de encastre (nesting) multi-plancha con rotación automática 90°,
     en vez de reportar "no cupieron" sin resolver el problema.
  3. Costo de material calculado sobre planchas realmente usadas (con
     % de merma/desperdicio), no sobre la suma de áreas de las piezas.
  4. Cantidad (copias) por diseño, IVA, descuento y utilidad claramente
     separados y editables.
  5. Tarifario de materiales editable en vivo (alta de materiales nuevos)
     en vez de un diccionario fijo en el código.
  6. Cotización exportable en PDF con membrete de Remeciendo, lista para
     enviar al cliente.

Dependencias nuevas: fpdf2 (pip install fpdf2)
"""

import io
import hashlib
import math
import os
import tempfile
from datetime import date

import shutil
import subprocess

import cv2
import ezdxf
import ezdxf.path
import matplotlib.patches as mpatches
import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pdfplumber
import streamlit as st
import svgpathtools
from shapely.geometry import Polygon as ShpPolygon

try:
    from fpdf import FPDF
    PDF_OK = True
except ImportError:
    PDF_OK = False

APP_VERSION = "v1.4.0"

st.set_page_config(
    page_title="Remeciendo — Cotizador Láser PRO",
    page_icon="⚡",
    layout="wide",
)


# ------------------------------------------------------------------
# Tarifas base (editables en la sidebar con st.data_editor)
# ------------------------------------------------------------------
DEFAULT_RATES = pd.DataFrame(
    [
        {"Material": "MDF 3mm", "corte_$/mm": 0.08, "grab_$/cm2": 0.05, "mat_$/m2": 4500, "vel_mm/s": 25},
        {"Material": "MDF 5.5mm", "corte_$/mm": 0.14, "grab_$/cm2": 0.05, "mat_$/m2": 7800, "vel_mm/s": 15},
        {"Material": "Acrílico 3mm", "corte_$/mm": 0.18, "grab_$/cm2": 0.08, "mat_$/m2": 18500, "vel_mm/s": 18},
        {"Material": "Acrílico 5mm", "corte_$/mm": 0.32, "grab_$/cm2": 0.08, "mat_$/m2": 31000, "vel_mm/s": 9},
    ]
)

if "rates_df" not in st.session_state:
    st.session_state.rates_df = DEFAULT_RATES.copy()

# ------------------------------------------------------------------
# Panel de configuración — a la derecha, con columnas nativas (responsivo)
# ------------------------------------------------------------------
# A diferencia de st.sidebar (que Streamlit fija siempre a la izquierda y
# que en pantallas angostas cambia de comportamiento por su cuenta), usar
# columnas nativas es 100% soportado y responsivo: en pantalla ancha el
# panel queda a la derecha; en el celular, Streamlit apila las columnas
# automáticamente (el panel pasa a verse debajo del contenido principal).
col_main, col_config = st.columns([2.4, 1], gap="large")

with col_config:
    st.header("⚙️ Configuración del Taller")
    st.caption("Cada sección es desplegable — ábrelas de a una, en el orden que prefieras.")

    with st.expander("🧱 Tarifario de materiales", expanded=True):
        st.caption("Puedes editar valores o agregar filas para nuevos materiales.")
        rates_edit = st.data_editor(
            st.session_state.rates_df,
            num_rows="dynamic",
            use_container_width=True,
            key="rates_editor",
        )
        st.session_state.rates_df = rates_edit
        material_sel = st.selectbox("Material de trabajo", rates_edit["Material"].tolist())
        mat_row = rates_edit[rates_edit["Material"] == material_sel].iloc[0]
        mat = {
            "corte_mm": float(mat_row["corte_$/mm"]),
            "grab_cm2": float(mat_row["grab_$/cm2"]),
            "mat_m2": float(mat_row["mat_$/m2"]),
            "vel": float(mat_row["vel_mm/s"]),
        }

    with st.expander("💵 Costos operativos y plancha", expanded=False):
        costo_minuto = st.number_input("Costo máquina ($/min)", value=350.0, step=25.0)
        costo_alistamiento = st.number_input("Setup fijo ($)", value=1500.0, step=250.0)
        margen_utilidad = st.slider("Margen de ganancia (%)", 0, 200, 40, step=5) / 100.0
        merma_pct = st.slider("Merma / desperdicio de plancha (%)", 0, 50, 15, step=5) / 100.0
        st.caption("La merma cubre recortes, bordes de sujeción y piezas de prueba.")

        st.markdown("**Plancha de material (mm)**")
        plancha_w = st.number_input("Ancho de plancha (X)", value=1200, step=100)
        plancha_h = st.number_input("Alto de plancha (Y)", value=900, step=100)
        margen_pieza = st.number_input("Separación entre piezas (mm)", value=5, step=1)
        permitir_rotar = st.checkbox("Permitir rotar piezas 90° para mejor encastre", value=True)

    with st.expander("🧾 Impuestos y condiciones comerciales", expanded=False):
        aplicar_iva = st.checkbox("Agregar IVA (19%)", value=True)
        descuento_pct = st.number_input("Descuento (%)", value=0.0, step=1.0, min_value=0.0, max_value=100.0) / 100.0

    with st.expander("👤 Datos de la cotización", expanded=False):
        cliente = st.text_input("Cliente", value="")
        n_cotizacion = st.text_input("N° de cotización", value=f"REM-{date.today().strftime('%Y%m%d')}-01")


# ------------------------------------------------------------------
# Parsers geométricos reales
# ------------------------------------------------------------------
def parse_dxf(f_bytes):
    """Devuelve (largo_corte_mm, ancho_mm, alto_mm) leyendo entidades DXF."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
        tmp.write(f_bytes)
        tmp_path = tmp.name
    try:
        doc = ezdxf.readfile(tmp_path)
        pts = []
        corte = 0.0
        for e in doc.modelspace():
            t = e.dxftype()
            if t == "LINE":
                corte += math.dist((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
                pts.extend([[e.dxf.start.x, e.dxf.start.y], [e.dxf.end.x, e.dxf.end.y]])
            elif t == "CIRCLE":
                corte += 2 * math.pi * e.dxf.radius
                cx, cy, r = e.dxf.center.x, e.dxf.center.y, e.dxf.radius
                pts.extend([[cx - r, cy - r], [cx + r, cy + r]])
            elif t == "ARC":
                r = e.dxf.radius
                ang = math.radians(e.dxf.end_angle - e.dxf.start_angle) % (2 * math.pi)
                corte += r * ang
                cx, cy = e.dxf.center.x, e.dxf.center.y
                pts.extend([[cx - r, cy - r], [cx + r, cy + r]])
            elif t in ("LWPOLYLINE", "POLYLINE"):
                vertices = list(e.get_points()) if t == "LWPOLYLINE" else [
                    (v.dxf.location.x, v.dxf.location.y) for v in e.vertices
                ]
                coords = [(v[0], v[1]) for v in vertices]
                for i in range(len(coords) - 1):
                    corte += math.dist(coords[i], coords[i + 1])
                if getattr(e, "closed", False) and len(coords) > 1:
                    corte += math.dist(coords[-1], coords[0])
                pts.extend([list(c) for c in coords])
        if pts:
            arr = np.array(pts)
            w = arr[:, 0].max() - arr[:, 0].min()
            h = arr[:, 1].max() - arr[:, 1].min()
            return max(corte, 1.0), max(w, 1.0), max(h, 1.0)
    except Exception as ex:
        st.warning(f"No se pudo leer el DXF completamente ({ex}). Se usan valores estimados.")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return 800.0, 100.0, 100.0


def parse_svg(f_bytes):
    """Devuelve (largo_corte_mm, ancho_mm, alto_mm) leyendo paths SVG.
    Asume que las unidades del SVG (viewBox/px) equivalen a mm — es el caso
    típico de exportaciones desde LightBurn/Illustrator/Inkscape con perfil mm."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
        tmp.write(f_bytes)
        tmp_path = tmp.name
    try:
        paths, _ = svgpathtools.svg2paths(tmp_path)
        if not paths:
            return 650.0, 120.0, 80.0
        corte = sum(p.length(error=1e-3) for p in paths if len(p) > 0)
        xmins, xmaxs, ymins, ymaxs = [], [], [], []
        for p in paths:
            if len(p) == 0:
                continue
            xmin, xmax, ymin, ymax = p.bbox()
            xmins.append(xmin); xmaxs.append(xmax)
            ymins.append(ymin); ymaxs.append(ymax)
        w = max(xmaxs) - min(xmins)
        h = max(ymaxs) - min(ymins)
        return max(corte, 1.0), max(w, 1.0), max(h, 1.0)
    except Exception as ex:
        st.warning(f"No se pudo leer el SVG completamente ({ex}). Se usan valores estimados.")
        return 650.0, 120.0, 80.0
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def parse_pdf(f_bytes, page_index=0):
    """Devuelve (largo_corte_mm, ancho_mm, alto_mm) leyendo objetos vectoriales
    (líneas, curvas y rectángulos) de un PDF con pdfplumber.
    Los PDF miden en puntos (1 pt = 1/72 in): se convierte a mm asumiendo
    que el diseño fue exportado a escala real 1:1 (lo normal en Illustrator/
    CorelDRAW/Inkscape al exportar para corte láser)."""
    PT_TO_MM = 25.4 / 72.0
    with pdfplumber.open(io.BytesIO(f_bytes)) as pdf:
        page = pdf.pages[page_index]
        corte_pt = 0.0
        xs, ys = [], []
        for ln in page.lines:
            corte_pt += math.dist((ln["x0"], ln["y0"]), (ln["x1"], ln["y1"]))
            xs.extend([ln["x0"], ln["x1"]]); ys.extend([ln["y0"], ln["y1"]])
        for rc in page.rects:
            corte_pt += 2 * ((rc["x1"] - rc["x0"]) + (rc["y1"] - rc["y0"]))
            xs.extend([rc["x0"], rc["x1"]]); ys.extend([rc["y0"], rc["y1"]])
        for cv in page.curves:
            pts = cv.get("pts", [])
            for i in range(len(pts) - 1):
                corte_pt += math.dist(pts[i], pts[i + 1])
            xs.extend([p[0] for p in pts]); ys.extend([p[1] for p in pts])

        if not xs:
            return 650.0, 120.0, 80.0

        corte_mm = corte_pt * PT_TO_MM
        ancho_mm = (max(xs) - min(xs)) * PT_TO_MM
        alto_mm = (max(ys) - min(ys)) * PT_TO_MM
        return max(corte_mm, 1.0), max(ancho_mm, 1.0), max(alto_mm, 1.0)


def parse_ai(f_bytes):
    """Los .ai modernos (desde Illustrator CS en adelante) son, por dentro,
    un PDF válido (compatibilidad PDF activada por defecto al guardar).
    Se intenta leer como PDF; si el archivo es un .ai "legacy" sin ese
    modo, se avisa y se usan valores estimados."""
    try:
        return parse_pdf(f_bytes)
    except Exception as ex:
        st.warning(
            f"No se pudo leer este .ai como PDF ({ex}). Vuelve a guardarlo desde "
            "Illustrator con 'Crear PDF compatible' activado, o expórtalo como "
            "SVG/DXF/PDF. Se usan valores estimados."
        )
        return 650.0, 120.0, 80.0


def parse_eps(f_bytes):
    """El formato EPS (PostScript) no tiene una librería pura de Python
    confiable para extraer vectores. Si el servidor tiene Ghostscript
    instalado (binario 'gs'), se convierte a PDF internamente y se reutiliza
    el parser de PDF; si no, se avisa y se usan valores estimados."""
    gs_bin = shutil.which("gs") or shutil.which("gswin64c")
    if not gs_bin:
        st.warning(
            "Este servidor no tiene Ghostscript instalado, así que no se puede "
            "leer el EPS con precisión. Se usan valores estimados — para mayor "
            "exactitud, exporta el diseño como PDF, SVG o DXF."
        )
        return 650.0, 120.0, 80.0

    with tempfile.NamedTemporaryFile(delete=False, suffix=".eps") as tmp_in:
        tmp_in.write(f_bytes)
        in_path = tmp_in.name
    out_path = in_path.replace(".eps", ".pdf")
    try:
        subprocess.run(
            [gs_bin, "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite", f"-sOutputFile={out_path}", in_path],
            check=True, timeout=60,
        )
        with open(out_path, "rb") as f:
            return parse_pdf(f.read())
    except Exception as ex:
        st.warning(f"No se pudo convertir el EPS con Ghostscript ({ex}). Se usan valores estimados.")
        return 650.0, 120.0, 80.0
    finally:
        for p in (in_path, out_path):
            if os.path.exists(p):
                os.remove(p)


def parse_raster(f_bytes, ancho_real_mm):
    """Para PNG/JPG: detecta el contorno externo del diseño y estima el
    perímetro de corte y las dimensiones reales, a partir del ancho real
    (mm) que indique el usuario (porque un píxel no tiene una escala fija)."""
    arr = np.frombuffer(f_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 650.0, ancho_real_mm, ancho_real_mm * 0.7
    _, th = cv2.threshold(img, 245, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 650.0, ancho_real_mm, ancho_real_mm * 0.7
    all_pts = np.vstack(contours)
    x, y, w_px, h_px = cv2.boundingRect(all_pts)
    escala = ancho_real_mm / w_px if w_px > 0 else 1.0
    perimetro_px = sum(cv2.arcLength(c, True) for c in contours)
    corte_mm = perimetro_px * escala
    ancho_mm = w_px * escala
    alto_mm = h_px * escala
    return max(corte_mm, 1.0), max(ancho_mm, 1.0), max(alto_mm, 1.0)


# ------------------------------------------------------------------
# Motor de encastre multi-plancha con rotación
# ------------------------------------------------------------------
def nest_pieces(piezas, plancha_w, plancha_h, margen, permitir_rotar):
    """Shelf packing (Next-Fit Decreasing Height) con soporte multi-plancha
    y rotación 90° opcional. Devuelve lista de planchas, cada una con sus
    piezas colocadas (x, y, w, h, nombre) + piezas que no lograron ubicarse."""
    ordenadas = sorted(piezas, key=lambda p: max(p["w"], p["h"]), reverse=True)
    sheets = [[]]  # cada plancha = lista de shelves; shelf = dict(y,h,x_cursor)
    placed_by_sheet = [[]]
    sin_ubicar = []

    for p in ordenadas:
        w, h = p["w"], p["h"]
        if permitir_rotar and w < h:
            w, h = h, w  # preferir la orientación más "apaisada" -> shelves más bajos

        if w > plancha_w - 2 * margen and h > plancha_w - 2 * margen:
            sin_ubicar.append(p)
            continue
        if w > plancha_w - 2 * margen:
            w, h = h, w  # forzar rotación si de plano no entra en ese ancho

        placed = False
        # 1) intentar en un shelf existente de cualquier plancha ya abierta
        for s_idx, shelves in enumerate(sheets):
            for shelf in shelves:
                if shelf["x"] + w <= plancha_w - margen and h <= shelf["h"]:
                    placed_by_sheet[s_idx].append(
                        {"x": shelf["x"], "y": shelf["y"], "w": w, "h": h, "nombre": p["nombre"]}
                    )
                    shelf["x"] += w + margen
                    placed = True
                    break
            if placed:
                break

        if placed:
            continue

        # 2) abrir un shelf nuevo en la última plancha, si hay alto disponible
        shelves = sheets[-1]
        used_h = margen + sum(sh["h"] + margen for sh in shelves)
        if used_h + h <= plancha_h - margen and w <= plancha_w - 2 * margen:
            shelves.append({"y": used_h, "h": h, "x": margen + w + margen})
            placed_by_sheet[-1].append({"x": margen, "y": used_h, "w": w, "h": h, "nombre": p["nombre"]})
            continue

        # 3) abrir una plancha nueva
        sheets.append([{"y": margen, "h": h, "x": margen + w + margen}])
        placed_by_sheet.append([{"x": margen, "y": margen, "w": w, "h": h, "nombre": p["nombre"]}])

    return placed_by_sheet, sin_ubicar


# ------------------------------------------------------------------
# Corrector de Kerf (compensación de ancho de corte para uniones/aletas)
# ------------------------------------------------------------------
def _closed_polys_from_dxf(f_bytes, sag=0.15):
    """Extrae todos los contornos CERRADOS de un DXF como polígonos shapely.
    Usa ezdxf.path para 'aplanar' arcos/bulges a segmentos rectos."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dxf") as tmp:
        tmp.write(f_bytes)
        tmp_path = tmp.name
    polys = []
    try:
        doc = ezdxf.readfile(tmp_path)
        for e in doc.modelspace():
            t = e.dxftype()
            is_closed_candidate = (
                (t in ("LWPOLYLINE", "POLYLINE") and getattr(e, "closed", False))
                or t == "CIRCLE"
            )
            if not is_closed_candidate:
                continue
            try:
                path = ezdxf.path.make_path(e)
                pts = [(v.x, v.y) for v in path.flattening(sag)]
                if len(pts) >= 3:
                    poly = ShpPolygon(pts)
                    if poly.is_valid and poly.area > 1e-6:
                        polys.append(poly)
            except Exception:
                continue
    except Exception as ex:
        st.warning(f"No se pudo leer geometría cerrada del DXF ({ex}).")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return polys


def _closed_polys_from_svg(f_bytes, n_samples=200):
    """Extrae contornos cerrados de un SVG como polígonos shapely."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".svg") as tmp:
        tmp.write(f_bytes)
        tmp_path = tmp.name
    polys = []
    try:
        paths, _ = svgpathtools.svg2paths(tmp_path)
        for p in paths:
            if len(p) == 0:
                continue
            if not p.isclosed():
                continue
            pts = [p.point(t / n_samples) for t in range(n_samples + 1)]
            pts = [(pt.real, pt.imag) for pt in pts]
            if len(pts) >= 3:
                poly = ShpPolygon(pts)
                if poly.is_valid and poly.area > 1e-6:
                    polys.append(poly)
    except Exception as ex:
        st.warning(f"No se pudo leer geometría cerrada del SVG ({ex}).")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return polys


def _closed_polys_from_pdf(f_bytes, page_index=0):
    """Extrae contornos cerrados de un PDF (o de un .ai/.eps ya convertidos
    a PDF) como polígonos shapely, usando los objetos vectoriales reales
    ('rect' y 'curve' cerrados) que entrega pdfplumber."""
    polys = []
    pt_to_mm = 25.4 / 72.0
    try:
        with pdfplumber.open(io.BytesIO(f_bytes)) as pdf:
            page = pdf.pages[page_index]
            for rc in page.rects:
                pts = rc.get("pts", [])
                if len(pts) >= 3:
                    poly = ShpPolygon([(x * pt_to_mm, y * pt_to_mm) for x, y in pts])
                    if poly.is_valid and poly.area > 1e-6:
                        polys.append(poly)
            for cv in page.curves:
                pts = cv.get("pts", [])
                path_ops = cv.get("path", [])
                cerrado = (path_ops and path_ops[-1][0] == "h") or (
                    len(pts) >= 3 and math.dist(pts[0], pts[-1]) < 1e-3
                )
                if cerrado and len(pts) >= 3:
                    poly = ShpPolygon([(x * pt_to_mm, y * pt_to_mm) for x, y in pts])
                    if poly.is_valid and poly.area > 1e-6:
                        polys.append(poly)
    except Exception as ex:
        st.warning(f"No se pudo leer geometría cerrada del PDF ({ex}).")
    return polys


def _closed_polys_from_raster(f_bytes, ancho_real_mm):
    """Extrae contornos cerrados de PNG/JPG y los escala a milímetros.
    Para archivos raster la escala no viene incorporada: el usuario entrega el
    ancho real de la pieza. Se conservan contornos exteriores e interiores para
    poder identificar ranuras igual que en un archivo vectorial."""
    arr = np.frombuffer(f_bytes, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    _, binary = cv2.threshold(image, 245, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    all_points = np.vstack(contours)
    _, _, width_px, _ = cv2.boundingRect(all_points)
    if width_px <= 0:
        return []
    scale = ancho_real_mm / width_px
    polys = []
    for contour in contours:
        if len(contour) < 3 or cv2.contourArea(contour) < 4:
            continue
        points = [(float(p[0][0]) * scale, float(p[0][1]) * scale) for p in contour]
        poly = ShpPolygon(points)
        if poly.is_valid and poly.area > 1e-6:
            polys.append(poly)
    return polys


def _closed_polys_from_ai_or_eps(f_bytes, filename):
    """.ai moderno = PDF por dentro; .eps se convierte primero a PDF con
    Ghostscript si está disponible. Devuelve lista de polígonos cerrados."""
    name = filename.lower()
    if name.endswith(".ai"):
        try:
            return _closed_polys_from_pdf(f_bytes)
        except Exception:
            st.warning(
                "No se pudo leer este .ai como PDF para el corrector de kerf "
                "(puede ser un .ai antiguo sin compatibilidad PDF)."
            )
            return []
    if name.endswith(".eps"):
        gs_bin = shutil.which("gs") or shutil.which("gswin64c")
        if not gs_bin:
            st.warning(
                "Este EPS necesita Ghostscript en el servidor para usarse en el "
                "corrector de kerf (agrega `ghostscript` a packages.txt)."
            )
            return []
        with tempfile.NamedTemporaryFile(delete=False, suffix=".eps") as tmp_in:
            tmp_in.write(f_bytes)
            in_path = tmp_in.name
        out_path = in_path.replace(".eps", ".pdf")
        try:
            subprocess.run(
                [gs_bin, "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite", f"-sOutputFile={out_path}", in_path],
                check=True, timeout=60,
            )
            with open(out_path, "rb") as f:
                return _closed_polys_from_pdf(f.read())
        except Exception as ex:
            st.warning(f"No se pudo convertir el EPS con Ghostscript ({ex}).")
            return []
        finally:
            for p in (in_path, out_path):
                if os.path.exists(p):
                    os.remove(p)
    return []


# ------------------------------------------------------------------
# Corrector de ranuras (slots) para uniones snap-fit / slot-tab
# ------------------------------------------------------------------
def _tipo_contorno(poly, polys):
    """Misma regla par/impar que usa el corrector de kerf: exterior si un
    número PAR de otros contornos lo contienen, agujero si es impar."""
    contenido_por = sum(1 for other in polys if other is not poly and other.contains(poly))
    return "exterior" if contenido_por % 2 == 0 else "agujero"


def detectar_ranuras(polys):
    """Detecta las ranuras (agujeros) de una pieza y mide su ancho, largo
    y ángulo real usando el rectángulo rotado mínimo que las contiene —
    así funciona igual de bien si la ranura está rotada, no solo si está
    perfectamente alineada a los ejes."""
    ranuras = []
    for i, poly in enumerate(polys):
        if _tipo_contorno(poly, polys) != "agujero":
            continue
        mrr = poly.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)[:-1]
        if len(coords) != 4:
            continue
        p0, p1, p2 = np.array(coords[0]), np.array(coords[1]), np.array(coords[2])
        lado_a = np.linalg.norm(p1 - p0)
        lado_b = np.linalg.norm(p2 - p1)
        largo, ancho = max(lado_a, lado_b), min(lado_a, lado_b)
        # ángulo del lado más largo (el eje "largo" de la ranura)
        if lado_a >= lado_b:
            ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
        else:
            ang = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
        centro = poly.centroid
        ranuras.append(
            {
                "idx": i,
                "poly": poly,
                "centro": (centro.x, centro.y),
                "angulo_deg": ang,
                "ancho_mm": ancho,
                "largo_mm": largo,
            }
        )
    return ranuras


def _rectangulo_desde_medidas(centro, angulo_deg, ancho, largo):
    """Construye un rectángulo (polígono shapely) centrado en 'centro',
    rotado 'angulo_deg', con el 'largo' medido a lo largo de ese ángulo
    y el 'ancho' perpendicular a él."""
    cx, cy = centro
    ang = math.radians(angulo_deg)
    hl, hw = largo / 2.0, ancho / 2.0
    esquinas_local = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw)]
    esquinas = []
    for x, y in esquinas_local:
        rx = cx + x * math.cos(ang) - y * math.sin(ang)
        ry = cy + x * math.sin(ang) + y * math.cos(ang)
        esquinas.append((rx, ry))
    return ShpPolygon(esquinas)


def aplicar_correccion_ranuras(polys, ranuras_corregidas):
    """Devuelve la lista completa de contornos (exteriores sin tocar +
    agujeros reemplazados por su versión corregida) lista para exportar."""
    idx_a_nuevo = {r["idx"]: r["poly_nuevo"] for r in ranuras_corregidas}
    resultado = []
    for i, poly in enumerate(polys):
        resultado.append(idx_a_nuevo.get(i, poly))
    return resultado


def clasificar_y_compensar(polys, kerf_mm, modo):
    """Clasifica cada contorno como 'exterior' o 'agujero' según cuántos otros
    contornos lo contienen (regla par/impar), y le aplica el offset de kerf
    correcto físicamente:
      - Exterior: +kerf/2  (el corte se come kerf/2 hacia adentro del material)
      - Agujero : -kerf/2  (el corte agranda el agujero kerf/2 hacia afuera)
    'modo' puede ser: 'completo', 'solo_agujeros', 'solo_exterior'."""
    resultado = []
    for i, poly in enumerate(polys):
        contenido_por = sum(
            1 for j, other in enumerate(polys) if j != i and other.contains(poly)
        )
        tipo = "exterior" if contenido_por % 2 == 0 else "agujero"

        offset = kerf_mm / 2.0
        if tipo == "agujero":
            offset = -offset
        if modo == "solo_agujeros" and tipo == "exterior":
            offset = 0.0
        if modo == "solo_exterior" and tipo == "agujero":
            offset = 0.0

        try:
            corregido = poly.buffer(offset, join_style=2, mitre_limit=5.0) if offset != 0 else poly
            if corregido.geom_type != "Polygon" or corregido.is_empty:
                corregido = poly  # el offset dejó geometría inválida: no tocar
        except Exception:
            corregido = poly

        bbox_o = poly.bounds
        bbox_c = corregido.bounds
        resultado.append(
            {
                "tipo": tipo,
                "original": poly,
                "corregido": corregido,
                "ancho_antes": bbox_o[2] - bbox_o[0],
                "alto_antes": bbox_o[3] - bbox_o[1],
                "ancho_despues": bbox_c[2] - bbox_c[0],
                "alto_despues": bbox_c[3] - bbox_c[1],
            }
        )
    return resultado


def exportar_dxf_corregido(resultado):
    """Genera bytes de un DXF con los contornos ya corregidos por kerf."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    doc.layers.add("EXTERIOR", color=5)
    doc.layers.add("AGUJEROS", color=1)
    for item in resultado:
        coords = list(item["corregido"].exterior.coords)
        layer = "EXTERIOR" if item["tipo"] == "exterior" else "AGUJEROS"
        msp.add_lwpolyline(coords, close=True, dxfattribs={"layer": layer})
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


with col_main:
    # ------------------------------------------------------------------
    # CSS responsivo liviano: reduce tamaños en pantallas angostas y deja
    # que la fila de pestañas se pueda deslizar con el dedo si no caben
    # todas, en vez de cortarse. (Esto NO fija posiciones ni pelea con el
    # layout interno de Streamlit como el truco de la sidebar que
    # abandonamos — solo ajusta tipografía y overflow, así que es seguro.)
    st.markdown(
        """
        <style>
        div[data-testid="stTabs"] div[role="tablist"] {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            flex-wrap: nowrap;
        }
        div[data-testid="stTabs"] button[role="tab"] {
            white-space: nowrap;
        }
        @media (max-width: 640px) {
            h1 { font-size: 1.6rem !important; }
            div[data-testid="stMetricValue"] { font-size: 1.1rem !important; }
            div[data-testid="stMetricLabel"] { font-size: 0.75rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ------------------------------------------------------------------
    # Encabezado centrado (menubar superior con la marca)
    # ------------------------------------------------------------------
    st.markdown(
        f"""
        <div style="text-align:center; padding-top: 0.5rem;">
            <span style="font-size: 2.4rem;">⚡</span>
            <h1 style="display:inline; margin-left: 0.4rem;">Remeciendo</h1>
            <p style="color: #888; margin-top: 0.2rem;">Cotizador y Nesting Láser · Estudio &amp; Taller Laser</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write(
        "Sube uno o varios archivos (DXF, SVG, PDF, AI, EPS, PNG, JPG) para cotizar y optimizar el uso de material."
    )

    archivos = st.file_uploader(
        "Carga tus diseños",
        accept_multiple_files=True,
        type=["dxf", "svg", "pdf", "ai", "eps", "png", "jpg", "jpeg"],
    )

    if archivos:
        st.subheader("Cantidad y tamaño real de cada diseño")
        st.caption(
            "Para PNG/JPG indica el ancho real en mm de la pieza terminada "
            "(los píxeles no tienen escala propia); para DXF/SVG/PDF/AI se detecta solo "
            "(EPS necesita Ghostscript en el servidor; si no está, se estima)."
        )

        piezas_meta = []
        for arch in archivos:
            name_lower = arch.name.lower()
            col1, col2 = st.columns([2, 1])
            with col1:
                cantidad = st.number_input(f"Cantidad — {arch.name}", min_value=1, value=1, step=1, key=f"qty_{arch.name}")
            with col2:
                ancho_real = None
                if name_lower.endswith((".png", ".jpg", ".jpeg")):
                    ancho_real = st.number_input(
                        f"Ancho real (mm) — {arch.name}", min_value=1.0, value=100.0, step=1.0, key=f"w_{arch.name}"
                    )
            piezas_meta.append({"archivo": arch, "cantidad": int(cantidad), "ancho_real": ancho_real})

        piezas = []
        corte_total = 0.0
        area_total_cm2 = 0.0

        for meta in piezas_meta:
            arch = meta["archivo"]
            bytes_data = arch.getvalue()
            name = arch.name.lower()
            if name.endswith(".dxf"):
                c, w, h = parse_dxf(bytes_data)
            elif name.endswith(".svg"):
                c, w, h = parse_svg(bytes_data)
            elif name.endswith(".pdf"):
                c, w, h = parse_pdf(bytes_data)
            elif name.endswith(".ai"):
                c, w, h = parse_ai(bytes_data)
            elif name.endswith(".eps"):
                c, w, h = parse_eps(bytes_data)
            else:
                c, w, h = parse_raster(bytes_data, meta["ancho_real"])

            for i in range(meta["cantidad"]):
                corte_total += c
                area_total_cm2 += (w * h) / 100.0
                piezas.append({"nombre": f"{arch.name}#{i+1}", "w": w, "h": h, "corte": c})

        # ---- Nesting y costos: se calculan ANTES de las pestañas para poder
        # mostrar un panel de indicadores (estilo dashboard) siempre visible,
        # sin importar en qué pestaña esté el usuario ----
        placed_by_sheet, sin_ubicar = nest_pieces(piezas, plancha_w, plancha_h, margen_pieza, permitir_rotar)
        n_planchas = len(placed_by_sheet)
        area_planchas_cm2 = n_planchas * (plancha_w * plancha_h) / 100.0
        area_piezas_cm2 = area_total_cm2
        utilizacion_pct = (area_piezas_cm2 / area_planchas_cm2 * 100.0) if area_planchas_cm2 > 0 else 0.0

        tiempo_maq_min = (corte_total / mat["vel"]) / 60.0
        # Costo de material: sobre planchas REALMENTE necesarias (con merma), no sobre el área de piezas.
        costo_mat = n_planchas * (plancha_w * plancha_h / 10000.0) * mat["mat_m2"] * (1 + merma_pct)
        costo_maq = tiempo_maq_min * costo_minuto
        costo_desgaste = corte_total * mat["corte_mm"]
        costo_neto = costo_mat + costo_maq + costo_desgaste + costo_alistamiento
        precio_con_utilidad = costo_neto * (1 + margen_utilidad)
        descuento_monto = precio_con_utilidad * descuento_pct
        subtotal = precio_con_utilidad - descuento_monto
        iva_monto = subtotal * 0.19 if aplicar_iva else 0.0
        total_final = subtotal + iva_monto

        # ---- Panel de indicadores (dashboard) — siempre visible ----
        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        kpi1.metric("Largo de corte total", f"{corte_total:,.0f} mm")
        kpi2.metric("Planchas necesarias", f"{n_planchas}")
        kpi3.metric("Utilización de plancha", f"{utilizacion_pct:.0f}%")
        kpi4.metric("VALOR TOTAL A COBRAR", f"${total_final:,.0f}")
        st.divider()

        tab1, tab2, tab3, tab4, tab5 = st.tabs(
            [
                "💰 Cotización",
                "🧩 Nesting",
                "📄 Exportar",
                "🔧 Kerf",
                "📐 Ranuras",
            ]
        )

        with tab1:
            st.subheader("Resumen de costos del lote")

            if sin_ubicar:
                st.error(
                    f"⚠️ {len(sin_ubicar)} pieza(s) no caben en el ancho de la plancha configurada "
                    f"({plancha_w}mm) ni rotadas. Revisa las dimensiones o aumenta el tamaño de plancha."
                )

            st.table(
                pd.DataFrame(
                    {
                        "Ítem": [
                            f"Material ({n_planchas} plancha(s), incl. {merma_pct*100:.0f}% merma)",
                            "Tiempo de corte/grabado (máquina)",
                            "Desgaste láser (según largo de corte)",
                            "Alistamiento máquina (setup)",
                            f"Utilidad ({margen_utilidad*100:.0f}%)",
                            f"Descuento (-{descuento_pct*100:.0f}%)",
                            "IVA (19%)" if aplicar_iva else "IVA (no aplica)",
                        ],
                        "Costo ($)": [
                            costo_mat,
                            costo_maq,
                            costo_desgaste,
                            costo_alistamiento,
                            precio_con_utilidad - costo_neto,
                            -descuento_monto,
                            iva_monto,
                        ],
                    }
                )
            )
            st.info(f"Utilización estimada de plancha: **{utilizacion_pct:.1f}%**")

        with tab2:
            st.subheader("Distribución automática de piezas por plancha")
            st.caption(
                f"{n_planchas} plancha(s) de {plancha_w}x{plancha_h}mm · "
                f"Rotación 90° {'activada' if permitir_rotar else 'desactivada'} · "
                f"Utilización global: {utilizacion_pct:.1f}%"
            )

            for idx, pzs in enumerate(placed_by_sheet):
                fig, ax = plt.subplots(figsize=(10, 7.5 * plancha_h / plancha_w if plancha_w else 5))
                ax.add_patch(
                    patches.Rectangle((0, 0), plancha_w, plancha_h, fill=False, edgecolor="black", lw=2)
                )
                for p in pzs:
                    ax.add_patch(
                        patches.Rectangle(
                            (p["x"], p["y"]), p["w"], p["h"],
                            fill=True, facecolor="#87CEFA", edgecolor="#4682B4", alpha=0.85, lw=1,
                        )
                    )
                    ax.text(
                        p["x"] + p["w"] / 2, p["y"] + p["h"] / 2, p["nombre"][:10],
                        ha="center", va="center", fontsize=6,
                    )
                ax.set_xlim(-20, plancha_w + 20)
                ax.set_ylim(-20, plancha_h + 20)
                ax.set_aspect("equal")
                ax.set_title(f"Plancha {idx + 1} de {n_planchas} — {len(pzs)} pieza(s)")
                st.pyplot(fig)

            if sin_ubicar:
                st.warning(
                    "Piezas que no se pudieron ubicar (demasiado grandes para la plancha): "
                    + ", ".join(p["nombre"] for p in sin_ubicar)
                )
            else:
                st.success("Todas las piezas fueron ubicadas en el número de planchas indicado arriba.")

        with tab3:
            st.subheader("Generar cotización en PDF")
            if not PDF_OK:
                st.error("Falta instalar la librería `fpdf2` (`pip install fpdf2`) para exportar en PDF.")
            else:
                if st.button("Generar PDF de la cotización"):
                    pdf = FPDF()
                    pdf.add_page()
                    pdf.set_font("Helvetica", "B", 16)
                    pdf.cell(0, 10, "Remeciendo Estudio & Taller Laser", ln=True)
                    pdf.set_font("Helvetica", "", 10)
                    pdf.cell(0, 6, "Pedro Aguirre Cerda, Santiago, Chile", ln=True)
                    pdf.ln(4)
                    pdf.set_font("Helvetica", "B", 12)
                    pdf.cell(0, 8, f"Cotización N° {n_cotizacion}", ln=True)
                    pdf.set_font("Helvetica", "", 10)
                    pdf.cell(0, 6, f"Fecha: {date.today().strftime('%d-%m-%Y')}", ln=True)
                    if cliente:
                        pdf.cell(0, 6, f"Cliente: {cliente}", ln=True)
                    pdf.ln(4)

                    pdf.set_font("Helvetica", "B", 10)
                    pdf.cell(90, 8, "Ítem", border=1)
                    pdf.cell(0, 8, "Monto ($)", border=1, ln=True)
                    pdf.set_font("Helvetica", "", 10)
                    filas = [
                        (f"Material ({n_planchas} plancha(s) de {material_sel})", costo_mat),
                        ("Tiempo de máquina", costo_maq),
                        ("Desgaste láser", costo_desgaste),
                        ("Alistamiento", costo_alistamiento),
                        (f"Utilidad ({margen_utilidad*100:.0f}%)", precio_con_utilidad - costo_neto),
                    ]
                    if descuento_pct > 0:
                        filas.append((f"Descuento ({descuento_pct*100:.0f}%)", -descuento_monto))
                    if aplicar_iva:
                        filas.append(("IVA (19%)", iva_monto))
                    for label, val in filas:
                        pdf.cell(90, 8, label, border=1)
                        pdf.cell(0, 8, f"{val:,.0f}", border=1, ln=True)

                    pdf.set_font("Helvetica", "B", 11)
                    pdf.cell(90, 9, "TOTAL", border=1)
                    pdf.cell(0, 9, f"${total_final:,.0f}", border=1, ln=True)

                    pdf_bytes = bytes(pdf.output(dest="S"))
                    st.download_button(
                        "⬇️ Descargar PDF",
                        data=pdf_bytes,
                        file_name=f"Cotizacion_{n_cotizacion}.pdf",
                        mime="application/pdf",
                    )

        with tab4:
            st.subheader("Corrector de kerf para uniones tipo caja (aletas/dientes)")
            st.caption(
                "El láser no corta con espesor cero: se lleva un ancho de material (kerf) centrado "
                "en la línea de vector. Eso hace que el contorno EXTERIOR de una pieza salga más "
                "angosto de lo dibujado, y que los AGUJEROS/ranuras salgan más anchos de lo dibujado. "
                "En una caja de aletas eso se traduce en dientes sueltos o ranuras apretadas. "
                "Esta herramienta agranda el contorno exterior en kerf/2 y achica los agujeros en "
                "kerf/2 para que la pieza cortada quede en la medida que diseñaste."
            )

            archivos_vectoriales = [
                a for a in archivos if a.name.lower().endswith((".dxf", ".svg", ".pdf", ".ai", ".eps"))
            ]

            if not archivos_vectoriales:
                st.info(
                    "El corrector de kerf trabaja sobre geometría vectorial cerrada "
                    "(DXF, SVG, PDF, AI o EPS). Los PNG/JPG no traen esa información, "
                    "así que no aplican aquí."
                )
            else:
                col_a, col_b, col_c = st.columns(3)
                with col_a:
                    archivo_kerf = st.selectbox(
                        "Archivo a corregir", [a.name for a in archivos_vectoriales]
                    )
                with col_b:
                    kerf_mm = st.number_input(
                        "Ancho de kerf (mm)", min_value=0.0, max_value=2.0, value=0.15, step=0.01,
                        help="Mídelo cortando un cuadrado de 50x50mm y viendo cuánto le falta al medirlo con pie de metro. "
                             "Típico: 0.10-0.15mm en MDF 3mm, 0.15-0.25mm en acrílico.",
                    )
                with col_c:
                    modo_kerf = st.selectbox(
                        "Modo de corrección",
                        ["completo", "solo_agujeros", "solo_exterior"],
                        format_func=lambda x: {
                            "completo": "Completo (exterior + agujeros)",
                            "solo_agujeros": "Solo ranuras/agujeros (no tocar tamaño exterior)",
                            "solo_exterior": "Solo contorno exterior",
                        }[x],
                    )

                arch_obj = next(a for a in archivos_vectoriales if a.name == archivo_kerf)
                bytes_data = arch_obj.getvalue()
                name_lower = archivo_kerf.lower()
                if name_lower.endswith(".dxf"):
                    polys_originales = _closed_polys_from_dxf(bytes_data)
                elif name_lower.endswith(".svg"):
                    polys_originales = _closed_polys_from_svg(bytes_data)
                elif name_lower.endswith(".pdf"):
                    polys_originales = _closed_polys_from_pdf(bytes_data)
                else:  # .ai / .eps
                    polys_originales = _closed_polys_from_ai_or_eps(bytes_data, archivo_kerf)

                if not polys_originales:
                    st.warning(
                        "No se encontraron contornos CERRADOS en este archivo (líneas sueltas, "
                        "polilíneas abiertas o splines no detectadas). Para que el corrector "
                        "funcione, cada pieza y cada agujero debe ser una polilínea/trazado cerrado."
                    )
                else:
                    resultado = clasificar_y_compensar(polys_originales, kerf_mm, modo_kerf)

                    st.markdown("#### Antes vs. Después")
                    fig, ax = plt.subplots(figsize=(8, 8))
                    for item in resultado:
                        xo, yo = item["original"].exterior.xy
                        ax.plot(xo, yo, "--", color="gray", linewidth=1, label="Original" if item is resultado[0] else None)
                        xc, yc = item["corregido"].exterior.xy
                        color = "#1f77b4" if item["tipo"] == "exterior" else "#d62728"
                        ax.plot(xc, yc, "-", color=color, linewidth=1.5)
                    ax.set_aspect("equal")
                    ax.set_title("Gris punteado = original · Azul = exterior corregido · Rojo = agujero corregido")
                    ax.legend(loc="upper right", fontsize=8)
                    st.pyplot(fig)

                    st.markdown("#### Verificación de medidas por contorno")
                    tabla = pd.DataFrame(
                        [
                            {
                                "Tipo": it["tipo"],
                                "Ancho antes (mm)": round(it["ancho_antes"], 2),
                                "Ancho después (mm)": round(it["ancho_despues"], 2),
                                "Alto antes (mm)": round(it["alto_antes"], 2),
                                "Alto después (mm)": round(it["alto_despues"], 2),
                            }
                            for it in resultado
                        ]
                    )
                    st.dataframe(tabla, use_container_width=True)

                    st.markdown("#### Ratificar contra una medida esperada")
                    st.caption(
                        "Si sabes que, por ejemplo, un diente/ranura debe medir exactamente cierto "
                        "ancho, ingrésalo aquí y compáralo con la fila correspondiente de la tabla de arriba."
                    )
                    col_x, col_y = st.columns(2)
                    with col_x:
                        medida_esperada = st.number_input("Medida esperada (mm)", min_value=0.0, value=10.0, step=0.1)
                    with col_y:
                        fila_idx = st.number_input(
                            "N° de fila de la tabla a comparar (0 = primera)",
                            min_value=0, max_value=len(resultado) - 1, value=0, step=1,
                        )
                    medida_real = resultado[int(fila_idx)]["ancho_despues"]
                    diff = medida_real - medida_esperada
                    if abs(diff) < 0.05:
                        st.success(f"✅ Coincide: {medida_real:.2f}mm vs {medida_esperada:.2f}mm esperados (dif. {diff:+.2f}mm).")
                    else:
                        st.warning(f"⚠️ Diferencia de {diff:+.2f}mm entre lo corregido ({medida_real:.2f}mm) y lo esperado ({medida_esperada:.2f}mm). Ajusta el valor de kerf e inténtalo de nuevo.")

                    dxf_corregido = exportar_dxf_corregido(resultado)
                    st.download_button(
                        "⬇️ Descargar DXF corregido (listo para cortar)",
                        data=dxf_corregido,
                        file_name=f"{archivo_kerf.rsplit('.', 1)[0]}_kerf_corregido.dxf",
                        mime="application/dxf",
                    )

        with tab5:
            st.subheader("Ranuras · corrector snap-fit / slot-tab")
            st.caption(
                "Editor de ranuras para uniones de madera tipo snap-fit. Ajusta medidas, "
                "posición y giro de cada ranura; conserva, modifica o elimina las que no "
                "usarás. La vista previa muestra solamente las ranuras."
            )

            archivos_ranuras = [
                a for a in archivos if a.name.lower().endswith(
                    (".dxf", ".svg", ".pdf", ".ai", ".eps", ".png", ".jpg", ".jpeg")
                )
            ]

            if not archivos_ranuras:
                st.info(
                    "Carga un archivo DXF, SVG, PDF, AI, EPS, PNG o JPG para corregir sus ranuras."
                )
            else:
                archivo_ranura = st.selectbox(
                    "Archivo a corregir", [a.name for a in archivos_ranuras], key="archivo_ranura_sel"
                )
                arch_obj_r = next(a for a in archivos_ranuras if a.name == archivo_ranura)
                bytes_data_r = arch_obj_r.getvalue()
                name_lower_r = archivo_ranura.lower()
                if name_lower_r.endswith(".dxf"):
                    polys_r = _closed_polys_from_dxf(bytes_data_r)
                elif name_lower_r.endswith(".svg"):
                    polys_r = _closed_polys_from_svg(bytes_data_r)
                elif name_lower_r.endswith(".pdf"):
                    polys_r = _closed_polys_from_pdf(bytes_data_r)
                elif name_lower_r.endswith((".png", ".jpg", ".jpeg")):
                    ancho_raster_ranuras = st.number_input(
                        "Ancho real de la pieza en la imagen (mm)", min_value=1.0,
                        value=100.0, step=1.0, key=f"ancho_ranuras::{archivo_ranura}",
                        help="Mide el ancho total de la pieza terminada. La aplicación usa esta medida para convertir píxeles a milímetros.",
                    )
                    polys_r = _closed_polys_from_raster(bytes_data_r, ancho_raster_ranuras)
                else:  # .ai / .eps
                    polys_r = _closed_polys_from_ai_or_eps(bytes_data_r, archivo_ranura)

                if not polys_r:
                    st.warning(
                        "No se encontraron contornos cerrados en este archivo. Revisa que "
                        "cada pieza y cada ranura sean trazados/polilíneas cerradas."
                    )
                else:
                    ranuras = detectar_ranuras(polys_r)
                    if not ranuras:
                        st.info(
                            "No se detectaron ranuras (agujeros interiores) en este archivo — "
                            "solo se encontró el contorno exterior de la(s) pieza(s)."
                        )
                    else:
                        st.markdown(f"#### {len(ranuras)} ranura(s) detectada(s)")
                        archivo_id = hashlib.sha1(bytes_data_r).hexdigest()[:12]
                        estado_key = f"ranuras_editor::{archivo_ranura}::{archivo_id}"
                        if estado_key not in st.session_state:
                            st.session_state[estado_key] = pd.DataFrame(
                                [
                                    {
                                        "Ranura": f"#{k + 1}",
                                        "Ancho actual (mm)": round(r["ancho_mm"], 2),
                                        "Largo actual (mm)": round(r["largo_mm"], 2),
                                        "Ancho (mm)": round(r["ancho_mm"], 2),
                                        "Largo (mm)": round(r["largo_mm"], 2),
                                        "Centro X (mm)": round(r["centro"][0], 2),
                                        "Centro Y (mm)": round(r["centro"][1], 2),
                                        "Giro (°)": round(r["angulo_deg"], 2),
                                        "Acción": "Editar",
                                    }
                                    for k, r in enumerate(ranuras)
                                ]
                            )
                        st.caption(
                            "Edita cualquier valor directamente. En Acción selecciona Conservar para "
                            "dejarla intacta, Editar para aplicar los valores, o Eliminar para quitarla "
                            "del DXF final."
                        )
                        tabla_editada = st.data_editor(
                            st.session_state[estado_key],
                            use_container_width=True,
                            key=f"tabla_ranuras_editor::{archivo_ranura}",
                            hide_index=True,
                            column_config={
                                "Ranura": st.column_config.TextColumn(disabled=True),
                                "Ancho actual (mm)": st.column_config.NumberColumn(disabled=True),
                                "Largo actual (mm)": st.column_config.NumberColumn(disabled=True),
                                "Ancho (mm)": st.column_config.NumberColumn(min_value=0.05, step=0.05, format="%.2f"),
                                "Largo (mm)": st.column_config.NumberColumn(min_value=0.05, step=0.05, format="%.2f"),
                                "Centro X (mm)": st.column_config.NumberColumn(step=0.1, format="%.2f"),
                                "Centro Y (mm)": st.column_config.NumberColumn(step=0.1, format="%.2f"),
                                "Giro (°)": st.column_config.NumberColumn(step=1.0, format="%.2f"),
                                "Acción": st.column_config.SelectboxColumn(
                                    options=["Conservar", "Editar", "Eliminar"], required=True
                                ),
                            },
                        )
                        st.session_state[estado_key] = tabla_editada.copy()

                        with st.expander("Aplicar medida estándar a las ranuras seleccionadas"):
                            col_g1, col_g2, col_g3, col_g4 = st.columns(4)
                            with col_g1:
                                ancho_global = st.number_input("Ancho para todas (mm)", min_value=0.1, value=2.0, step=0.1)
                            with col_g2:
                                largo_global = st.number_input("Largo para todas (mm)", min_value=0.1, value=35.0, step=0.5)
                            with col_g3:
                                accion_global = st.selectbox("Acción a aplicar", ["Editar", "Conservar", "Eliminar"])
                            with col_g4:
                                st.write("")
                                aplicar_global = st.button("Aplicar a todas", type="secondary")
                            if aplicar_global:
                                tabla_editada["Ancho (mm)"] = ancho_global
                                tabla_editada["Largo (mm)"] = largo_global
                                tabla_editada["Acción"] = accion_global
                                st.session_state[estado_key] = tabla_editada
                                st.rerun()

                        ranuras_corregidas = []
                        idx_eliminadas = set()
                        for k, r in enumerate(ranuras):
                            fila = tabla_editada.iloc[k]
                            accion = fila["Acción"]
                            if accion == "Eliminar":
                                idx_eliminadas.add(r["idx"])
                                continue
                            if accion == "Conservar":
                                continue
                            nuevo_ancho = float(fila["Ancho (mm)"])
                            nuevo_largo = float(fila["Largo (mm)"])
                            nuevo_centro = (float(fila["Centro X (mm)"]), float(fila["Centro Y (mm)"]))
                            poly_nuevo = _rectangulo_desde_medidas(
                                nuevo_centro, float(fila["Giro (°)"]), nuevo_ancho, nuevo_largo
                            )
                            ranuras_corregidas.append({"idx": r["idx"], "poly_nuevo": poly_nuevo})

                        st.markdown("#### Vista previa de ranuras")
                        fig_r, ax_r = plt.subplots(figsize=(8, 8))
                        idx_corregidas = {rc["idx"] for rc in ranuras_corregidas}
                        for r in ranuras:
                            xo, yo = r["poly"].exterior.xy
                            ax_r.plot(xo, yo, "--", color="gray", linewidth=1)
                        contornos_finales = aplicar_correccion_ranuras(polys_r, ranuras_corregidas)
                        for r in ranuras:
                            i = r["idx"]
                            if i in idx_eliminadas:
                                continue
                            poly = contornos_finales[i]
                            xc, yc = poly.exterior.xy
                            color = "#2ca02c" if i in idx_corregidas else "#1f77b4"
                            ax_r.plot(xc, yc, "-", color=color, linewidth=1.5)
                        ax_r.set_aspect("equal")
                        ax_r.set_title("Gris punteado = original · Verde = editada · Azul = conservada")
                        st.pyplot(fig_r)

                        st.caption(
                            f"Resultado: {len(idx_corregidas)} editada(s), "
                            f"{len(idx_eliminadas)} eliminada(s), "
                            f"{len(ranuras) - len(idx_corregidas) - len(idx_eliminadas)} conservada(s)."
                        )
                        st.markdown("#### Verificación final")
                        st.dataframe(
                            tabla_editada[["Ranura", "Ancho actual (mm)", "Largo actual (mm)", "Ancho (mm)", "Largo (mm)", "Acción"]],
                            use_container_width=True,
                            hide_index=True,
                        )

                        # Exportación: reconstruye el DXF marcando como "agujero" cada contorno que efectivamente es una ranura,
                        # y "exterior" el resto — usando la misma regla par/impar.
                        items_export = []
                        for i, poly in enumerate(contornos_finales):
                            if i in idx_eliminadas:
                                continue
                            tipo = "agujero" if _tipo_contorno(polys_r[i], polys_r) == "agujero" else "exterior"
                            items_export.append({"tipo": tipo, "corregido": poly})
                        dxf_ranuras = exportar_dxf_corregido(items_export)

                        st.download_button(
                            "⬇️ Descargar DXF con ranuras corregidas",
                            data=dxf_ranuras,
                            file_name=f"{archivo_ranura.rsplit('.', 1)[0]}_ranuras_corregidas.dxf",
                            mime="application/dxf",
                        )

    else:
        st.info("Sube al menos un archivo para comenzar.")


# ------------------------------------------------------------------
# Footer (footermenu con versión y copyright)
# ------------------------------------------------------------------
st.markdown(
    f"""
    <hr style="margin-top: 3rem; opacity: 0.2;">
    <div style="text-align:center; color:#999; font-size:0.85rem; padding-bottom: 1rem;">
        Remeciendo Estudio &amp; Taller Laser · {APP_VERSION}<br>
        © {date.today().year} Remeciendo — Todos los derechos reservados.
    </div>
    """,
    unsafe_allow_html=True,
)
