import streamlit as st
from py3dbp import Packer, Bin, Item
import plotly.graph_objects as go
import pandas as pd
from decimal import Decimal
import json
import firebase_admin
from firebase_admin import credentials, firestore
import urllib.parse
import tempfile
import os

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="3D Cargo Optimizer", layout="wide")

# --- FIREBASE CONNECTION ---
try:
    if not firebase_admin._apps:
        key_dict = json.loads(st.secrets["firebase_credentials"])
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    FIREBASE_ENABLED = True
except Exception as e:
    st.error(f"⚠️ Firebase configuration missing or invalid. Please check your Streamlit Secrets. Error: {e}")
    FIREBASE_ENABLED = False

# --- DATABASE FUNCTIONS ---
def load_library():
    """Load all products from the Firestore 'products' collection"""
    if not FIREBASE_ENABLED: return {}
    try:
        docs = db.collection(u'products').stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception as e:
        st.error(f"Database connection error: {e}")
        return {}

def save_library(library_data):
    """Save each product into the Firestore 'products' collection"""
    if not FIREBASE_ENABLED: return
    try:
        for key, value in library_data.items():
            db.collection(u'products').document(key).set(value)
    except Exception as e:
        st.error(f"Save error: {e}")

def load_configs():
    """Load saved cargo mixes from Firestore"""
    if not FIREBASE_ENABLED: return {}
    try:
        docs = db.collection(u'configs').stream()
        return {doc.id: doc.to_dict() for doc in docs}
    except Exception:
        return {}

def save_config(name, items):
    """Save a specific mix of cargo to Firestore"""
    if not FIREBASE_ENABLED: return
    try:
        db.collection(u'configs').document(name).set({"items": items})
    except Exception as e:
        st.error(f"Save error: {e}")

# Load library into memory
if 'product_lib' not in st.session_state:
    st.session_state.product_lib = load_library()
if 'cargo_items' not in st.session_state:
    st.session_state.cargo_items = []
if 'color_map' not in st.session_state:
    st.session_state.color_map = {}

# --- URL PARAMETERS (SHARE LINK HANDLING) ---
if 'config' in st.query_params and not st.session_state.get('loaded_from_url'):
    config_name = st.query_params['config']
    configs = load_configs()
    if config_name in configs:
        st.session_state.cargo_items = configs[config_name]['items']
        for item in st.session_state.cargo_items:
            st.session_state.color_map[item['Reference']] = item.get('Color', '#333333')
        st.session_state.loaded_from_url = True
        st.toast(f"✅ Configuration '{config_name}' loaded from shared link!")
    else:
        st.error("❌ Shared configuration not found in database.")

# --- CONTAINERS DATA ---
CONTAINERS = {
    "TIR (Semi-trailer)": {"L": 1360, "W": 245, "H": 270, "max_weight": 24000},
    "20FT Standard": {"L": 589, "W": 235, "H": 239, "max_weight": 28000},
    "40FT Standard": {"L": 1203, "W": 235, "H": 239, "max_weight": 28000},
    "40HQ (High Cube)": {"L": 1203, "W": 235, "H": 269, "max_weight": 28000}
}

DISTINCT_COLORS = [
    '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', 
    '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe', 
    '#008080', '#e6beff', '#9a6324', '#fffac8', '#800000'
]

# --- CUSTOM CLASSES & LOGISTICS LOGIC ---
class CustomItem(Item):
    def __init__(self, name, width, height, depth, weight, allowed_rotations, stackable):
        super().__init__(name, width, height, depth, weight)
        self.allowed_rotations = allowed_rotations 
        self.stackable = stackable                 

    def get_dimension(self):
        current_rotation = getattr(self, 'rotation_type', 0)
        if current_rotation not in self.allowed_rotations:
            return (Decimal('999999'), Decimal('999999'), Decimal('999999'))
        return super().get_dimension()

def custom_pack_item_to_bin(bin_obj, item):
    """Extreme Point Best Fit Algorithm for irregular dimensions."""
    pivots = set([(Decimal('0'), Decimal('0'), Decimal('0'))])
    for p in bin_obj.items:
        w, h, d = p.get_dimension()
        px, py, pz = p.position
        pivots.update([(px+w, py, pz), (px, py+h, pz), (px, py, pz+d)])
        for q in bin_obj.items:
            if p != q:
                qw, qh, qd = q.get_dimension()
                qx, qy, qz = q.position
                pivots.update([(px+w, qy, pz), (px, qy+qh, pz), (qx, py+h, pz), (qx+qw, py+h, pz)])

    valid_pivots = [p for p in pivots if p[0] < bin_obj.width and p[1] < bin_obj.height and p[2] < bin_obj.depth]
    sorted_pivots = sorted(valid_pivots, key=lambda p: (p[2], p[0], p[1]))

    best_score, best_pivot, best_rot = -9999999, None, None

    for pivot in sorted_pivots:
        for rot in item.allowed_rotations:
            item.rotation_type = rot
            w, h, d = item.get_dimension()
            
            if bin_obj.width < pivot[0] + w or bin_obj.height < pivot[1] + h or bin_obj.depth < pivot[2] + d: continue
            if sum(i.weight for i in bin_obj.items) + item.weight > bin_obj.max_weight: continue

            collision = False
            for p_item in bin_obj.items:
                pw, ph, pd = p_item.get_dimension()
                px, py, pz = p_item.position
                if not (pivot[0] >= px+pw or pivot[0]+w <= px or pivot[1] >= py+ph or pivot[1]+h <= py or pivot[2] >= pz+pd or pivot[2]+d <= pz):
                    collision = True; break
                if getattr(p_item, 'stackable', True) is False and pivot[2] >= pz+pd:
                    if not (pivot[0] >= px+pw or pivot[0]+w <= px or pivot[1] >= py+ph or pivot[1]+h <= py):
                        collision = True; break
                            
            if not collision:
                score = 0
                if pivot[0] == 0: score += float(h * d)
                if pivot[1] == 0: score += float(w * d)
                if pivot[2] == 0: score += float(w * h)
                if pivot[0] + w == bin_obj.width: score += float(h * d)
                if pivot[1] + h == bin_obj.height: score += float(w * d)
                if pivot[2] + d == bin_obj.depth: score += float(w * h)
                
                for p_item in bin_obj.items:
                    pw, ph, pd = p_item.get_dimension()
                    px, py, pz = p_item.position
                    if pivot[0] == px+pw or pivot[0]+w == px:
                        oy, oz = min(pivot[1]+h, py+ph) - max(pivot[1], py), min(pivot[2]+d, pz+pd) - max(pivot[2], pz)
                        if oy > 0 and oz > 0: score += float(oy * oz) * 2
                    if pivot[1] == py+ph or pivot[1]+h == py:
                        ox, oz = min(pivot[0]+w, px+pw) - max(pivot[0], px), min(pivot[2]+d, pz+pd) - max(pivot[2], pz)
                        if ox > 0 and oz > 0: score += float(ox * oz) * 2
                    if pivot[2] == pz+pd or pivot[2]+d == pz:
                        ox, oy = min(pivot[0]+w, px+pw) - max(pivot[0], px), min(pivot[1]+h, py+ph) - max(pivot[1], py)
                        if ox > 0 and oy > 0: score += float(ox * oy) * 2

                score -= (float(pivot[0]) + float(pivot[1]) + float(pivot[2])) * 0.1
                
                # --- NEW HEURISTIC RULE for 80cm ---
                # h is the dimension along the Y axis (width of the container).
                # If an edge is 80cm and the container fits at least 3 (>= 240), give a massive bonus to force rows of 3.
                if abs(float(h) - 80.0) <= 1.0 and float(bin_obj.height) >= 240.0:
                    score += 50000.0 
                
                if score > best_score:
                    best_score, best_pivot, best_rot = score, pivot, rot
                    
    if best_pivot is not None:
        item.rotation_type = best_rot
        item.position = best_pivot
        bin_obj.items.append(item)
        return True
    return False

def get_optimal_europallet_slots(container_name):
    """Hardcoded Pinwheel strategies for Europallets."""
    slots = []
    if "20FT" in container_name:
        for i in range(7): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(4): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "40FT" in container_name or "40HQ" in container_name:
        for i in range(15): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(10): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "TIR" in container_name:
        for i in range(11):
            for j in range(3): slots.append({'x': i*120, 'y': j*80, 'w': 120, 'h': 80, 'filled': False})
    return slots

def pack_with_rules(bin_obj, item, euro_slots):
    l, w = float(item.width), float(item.height)
    is_europallet = (abs(l-120) <= 1 and abs(w-80) <= 1) or (abs(l-80) <= 1 and abs(w-120) <= 1)
    
    if is_europallet:
        for slot in euro_slots:
            if not slot['filled']:
                if sum(i.weight for i in bin_obj.items) + item.weight > bin_obj.max_weight: continue 
                if float(item.depth) > float(bin_obj.depth): continue
                assigned = False
                for rot in item.allowed_rotations:
                    item.rotation_type = rot
                    d = item.get_dimension()
                    if abs(float(d[0]) - slot['w']) <= 1 and abs(float(d[1]) - slot['h']) <= 1:
                        
                        # --- Vérification stricte des collisions pour la grille Europalette ---
                        collision = False
                        p0, p1, p2 = Decimal(str(slot['x'])), Decimal(str(slot['y'])), Decimal('0')
                        w_d, h_d, d_d = d
                        
                        for p_item in bin_obj.items:
                            pw, ph, pd = p_item.get_dimension()
                            px, py, pz = p_item.position
                            if not (p0 >= px+pw or p0+w_d <= px or p1 >= py+ph or p1+h_d <= py or p2 >= pz+pd or p2+d_d <= pz):
                                collision = True; break
                                
                            if getattr(p_item, 'stackable', True) is False and p2 >= pz+pd:
                                if not (p0 >= px+pw or p0+w_d <= px or p1 >= py+ph or p1+h_d <= py):
                                    collision = True; break
                        
                        if not collision:
                            # --- NOUVEAU : Glissement (Gravity Slide) vers l'avant (Axe X) ---
                            # Permet de supprimer les espaces vides quand le chargement précédent n'est pas aligné sur la grille.
                            min_x = Decimal('0')
                            for p_item in bin_obj.items:
                                pw, ph, pd = p_item.get_dimension()
                                px, py, pz = p_item.position
                                # Vérifie si les articles sont sur la même "ligne" Y et Z
                                if not (p1 >= py+ph or p1+h_d <= py or p2 >= pz+pd or p2+d_d <= pz):
                                    # Si l'article existant est devant l'emplacement prévu sur l'axe X
                                    if px + pw <= p0:
                                        min_x = max(min_x, px + pw)
                            
                            # On écrase la coordonnée X fixe par la nouvelle coordonnée glissée
                            p0 = min_x
                            
                            item.position = (p0, p1, p2)
                            bin_obj.items.append(item)
                            assigned = True
                            slot['filled'] = True
                            break
                if assigned: return True
    return custom_pack_item_to_bin(bin_obj, item)

def plot_3d_packing(container_dim, fitted_items, color_map, title):
    """Generates 3D visualization."""
    fig = go.Figure()
    cx, cy, cz = container_dim

    x_lines = [0, cx, cx, 0, 0, None, 0, cx, cx, 0, 0, None, 0, 0, None, cx, cx, None, cx, cx, None, 0, 0]
    y_lines = [0, 0, cy, cy, 0, None, 0, 0, cy, cy, 0, None, 0, 0, None, 0, 0, None, cy, cy, None, cy, cy]
    z_lines = [0, 0, 0, 0, 0, None, cz, cz, cz, cz, cz, None, 0, cz, None, 0, cz, None, 0, cz, None, 0, cz]
    
    fig.add_trace(go.Scatter3d(x=x_lines, y=y_lines, z=z_lines, mode='lines', line=dict(color='gray', width=3), name="Container", hoverinfo='skip'))

    all_x_edges, all_y_edges, all_z_edges = [], [], []

    for item in fitted_items:
        ref_name = item.name.split(" #")[0]
        color = color_map.get(ref_name, '#333333')
        x, y, z = map(float, item.position)
        w, h, d = map(float, item.get_dimension())

        x_coords = [x, x+w, x+w, x, x, x+w, x+w, x]
        y_coords = [y, y, y+h, y+h, y, y, y+h, y+h]
        z_coords = [z, z, z, z, z+d, z+d, z+d, z+d]
        i_faces, j_faces, k_faces = [0, 0, 4, 4, 0, 0, 3, 3, 0, 0, 1, 1], [1, 2, 5, 6, 1, 5, 2, 6, 3, 7, 2, 6], [2, 3, 6, 7, 5, 4, 6, 7, 7, 4, 6, 5]

        emp_status = "✅ Yes" if getattr(item, 'stackable', True) else "❌ NO"
        hovertext = f"<b>{item.name}</b><br>Dim : {w}x{h}x{d} cm<br>Stackable : {emp_status}"

        fig.add_trace(go.Mesh3d(x=x_coords, y=y_coords, z=z_coords, i=i_faces, j=j_faces, k=k_faces, color=color, opacity=1.0, flatshading=True, name=ref_name, hoverinfo="text", text=hovertext, showscale=False))

        all_x_edges.extend([x, x+w, x+w, x, x, None, x, x+w, x+w, x, x, None, x, x, None, x+w, x+w, None, x+w, x+w, None, x, x, None])
        all_y_edges.extend([y, y, y+h, y+h, y, None, y, y, y+h, y+h, y, None, y, y, None, y, y, None, y+h, y+h, None, y+h, y+h, None])
        all_z_edges.extend([z, z, z, z, z, None, z+d, z+d, z+d, z+d, z+d, None, z, z+d, None, z, z+d, None, z, z+d, None, z, z+d, None])

    if all_x_edges:
        fig.add_trace(go.Scatter3d(x=all_x_edges, y=all_y_edges, z=all_z_edges, mode='lines', line=dict(color='black', width=3), hoverinfo='skip', showlegend=False))

    fig.update_layout(title=title, scene=dict(xaxis=dict(title='Length (cm)', range=[0, cx * 1.1]), yaxis=dict(title='Width (cm)', range=[0, cy * 1.1]), zaxis=dict(title='Height (cm)', range=[0, cz * 1.1]), aspectmode='data', camera=dict(eye=dict(x=1.5, y=1.5, z=1.5))), margin=dict(l=0, r=0, b=0, t=40), legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01))
    return fig

# --- PDF GENERATOR ---
def generate_pdf_report(cargo_items, used_bins, container_props):
    try:
        from fpdf import FPDF
    except ImportError:
        st.error("⚠️ The PDF Export feature requires 'fpdf2'. Please add it to your requirements.txt")
        return None

    has_kaleido = False
    try:
        import kaleido
        has_kaleido = True
    except ImportError:
        pass
    
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=16, style='B')
    pdf.cell(200, 10, txt="3D Cargo Optimization Report", ln=True, align='C')
    
    pdf.set_font("Arial", size=12, style='B')
    pdf.cell(200, 10, txt="Cargo Summary Table:", ln=True)
    pdf.set_font("Arial", size=10)
    
    for item in cargo_items:
        txt = f"- {item['Quantity']}x {item['Reference']} (Dim: {item['Length']}x{item['Width']}x{item['Height']}cm, {item['Weight']}kg)"
        pdf.cell(200, 8, txt=txt, ln=True)

    image_error_shown = False
    for b in used_bins:
        pdf.add_page()
        pdf.set_font("Arial", size=14, style='B')
        pdf.cell(200, 10, txt=f"Vehicle: {b.name}", ln=True)
        
        if has_kaleido:
            fig = plot_3d_packing((container_props["L"], container_props["W"], container_props["H"]), b.items, st.session_state.color_map, "")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmpfile:
                try:
                    # This can throw an error on Streamlit Cloud if Chromium is not installed
                    fig.write_image(tmpfile.name, engine="kaleido", scale=2)
                    pdf.image(tmpfile.name, x=10, y=30, w=190)
                except Exception as e:
                    pdf.set_font("Arial", size=10, style='I')
                    pdf.cell(200, 10, txt="[3D Image rendering unavailable in this cloud environment]", ln=True)
                    if not image_error_shown:
                        st.warning("⚠️ **Note:** Streamlit Cloud couldn't render the 3D images for the PDF (Chromium missing).")
                        image_error_shown = True
        else:
            pdf.set_font("Arial", size=10, style='I')
            pdf.cell(200, 10, txt="[3D Image rendering unavailable: 'kaleido' package not installed]", ln=True)
            if not image_error_shown:
                st.info("💡 To get 3D images in the PDF, the 'kaleido' package must be installed in requirements.txt (Note: it can be unstable on Streamlit Cloud).")
                image_error_shown = True
            
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
        pdf.output(tmp_pdf.name)
        return tmp_pdf.name

# --- USER INTERFACE ---
st.title("📦 3D Multi-Vehicle Cargo Optimizer")

col1, col2 = st.columns([1, 2])

with col1:
    st.header("1. Transport Parameters")
    transport_type = st.selectbox("Container / Truck Type", list(CONTAINERS.keys()))
    c_props = CONTAINERS[transport_type]
    max_bins = st.number_input("Maximum number of vehicles", min_value=1, max_value=10, value=2)
    st.info(f"Dimensions : {c_props['L']} x {c_props['W']} x {c_props['H']} cm | Max weight : {c_props['max_weight']} kg")

    st.header("2. Add Cargo")
    lib_keys = list(st.session_state.product_lib.keys())
    selected_preset = st.selectbox("📚 Load a product from library", ["-- New product --"] + lib_keys)
    
    # Defaults and backward compatibility for existing French database entries
    d_ref, d_l, d_h = "", 120.0, 100.0
    d_w, d_weight = 80.0, 500.0
    
    if selected_preset != "-- New product --":
        prod = st.session_state.product_lib[selected_preset]
        d_ref = prod.get("Ref", "")
        d_l = float(prod.get("L", 120.0))
        d_w = float(prod.get("W", prod.get("l", 80.0))) # Fallback for old "l"
        d_h = float(prod.get("H", 100.0))
        d_weight = float(prod.get("Weight", prod.get("Poids", 500.0))) # Fallback for old "Poids"
        
        rot_raw = prod.get("Rotation", "Auto (Horizontal)")
        if rot_raw in ["Horizontale", "Horizontal", "Auto (Horizontal)"]: rot_en = "Auto (Horizontal)"
        elif rot_raw in ["Toutes", "All", "Auto (All)"]: rot_en = "Auto (All)"
        elif rot_raw in ["Aucune", "None", "Strict: L -> Length"]: rot_en = "Strict: L -> Length"
        elif rot_raw == "Strict: W -> Length": rot_en = "Strict: W -> Length"
        else: rot_en = "Auto (Horizontal)"
        
        rot_mapping = {"Auto (Horizontal)": 0, "Auto (All)": 1, "Strict: L -> Length": 2, "Strict: W -> Length": 3}
        d_rot = rot_mapping.get(rot_en, 0)
        
        stack_raw = prod.get("Stackable", prod.get("Empilable", "Yes"))
        d_stack = 0 if stack_raw in ["Yes", "Oui"] else 1
    else:
        d_rot = 0
        d_stack = 0

    with st.form("add_item_form", clear_on_submit=False):
        ref = st.text_input("Reference", value=d_ref)
        col_qty, col_prio = st.columns(2)
        qty = col_qty.number_input("Quantity", min_value=1, value=1, step=1)
        prio = col_prio.number_input("Priority (1 = First)", min_value=1, value=len(st.session_state.cargo_items)+1, step=1)
        
        st.markdown("**Enter values in CENTIMETERS!**")
        cl, cw, ch = st.columns(3)
        l = cl.number_input("Length", min_value=1.0, value=d_l, step=1.0)
        w = cw.number_input("Width", min_value=1.0, value=d_w, step=1.0)
        h = ch.number_input("Height", min_value=1.0, value=d_h, step=1.0)
        weight = st.number_input("Unit Weight (kg)", min_value=0.1, value=d_weight, step=10.0)
        
        rotation_policy = st.radio("Allowed Rotation", ["Auto (Horizontal)", "Auto (All)", "Strict: L -> Length", "Strict: W -> Length"], index=d_rot, horizontal=False)
        stackable = st.radio("Stackable?", ["Yes", "No"], index=d_stack, horizontal=True)
        save_to_lib = st.checkbox("💾 Save product to library", value=False)
        submit = st.form_submit_button("Add to list")

        if submit:
            if ref:
                color = DISTINCT_COLORS[len(st.session_state.color_map) % len(DISTINCT_COLORS)]
                if ref not in st.session_state.color_map:
                    st.session_state.color_map[ref] = color
                else:
                    color = st.session_state.color_map[ref]
                
                st.session_state.cargo_items.append({
                    "Priority": int(prio), "Reference": ref, "Quantity": qty, 
                    "Length": l, "Width": w, "Height": h, "Weight": weight,
                    "Rotation": rotation_policy, "Stackable": stackable, "Color": color
                })
                
                if save_to_lib:
                    st.session_state.product_lib[ref] = {
                        "Ref": ref, "L": l, "W": w, "H": h, "Weight": weight, 
                        "Rotation": rotation_policy, "Stackable": stackable
                    }
                    save_library(st.session_state.product_lib)
                    st.success(f"'{ref}' saved in library!")
                else:
                    st.success(f"Added : {qty}x {ref}")
            else:
                st.error("Please enter a reference.")

    st.markdown("---")
    st.header("💾 Configuration Management")
    
    # Save Section
    save_name = st.text_input("Name this Cargo Mix to save (e.g., 'Weekly Order A')")
    if st.button("💾 Save Current Mix", use_container_width=True):
        if save_name and st.session_state.cargo_items:
            save_config(save_name, st.session_state.cargo_items)
            st.success(f"Mix '{save_name}' Saved successfully!")
        else:
            st.error("Enter a name and add items to the list first.")
            
    st.write("") # Small spacing
    
    # Load / Share Section
    configs = load_configs()
    if configs:
        selected_mix = st.selectbox("📂 Load or Share a saved mix", ["-- Select a mix --"] + list(configs.keys()))
        c_l, c_sh = st.columns(2)
        
        with c_l:
            if st.button("📂 Load Mix", use_container_width=True):
                if selected_mix != "-- Select a mix --":
                    st.session_state.cargo_items = configs[selected_mix]['items']
                    for item in st.session_state.cargo_items:
                        st.session_state.color_map[item['Reference']] = item.get('Color', '#333333')
                    st.rerun()
                else:
                    st.error("Please select a mix first.")
        with c_sh:
            if st.button("🔗 Share Link", use_container_width=True):
                if selected_mix != "-- Select a mix --":
                    BASE_URL = "https://optimiseur-3d-frozenbytes.streamlit.app/"
                    st.info(f"Send this link to your friend:\n\n**{BASE_URL}?config={urllib.parse.quote(selected_mix)}**")
                else:
                    st.error("Please select a mix first.")
    else:
        st.info("No saved configurations available yet.")

    # --- LIBRARY EDITOR ---
    st.markdown("---")
    with st.expander("⚙️ Library Editor (Edit / Delete products)"):
        lib_data = []
        for k, v in st.session_state.product_lib.items():
            # Apply backward compatibility here as well
            rot_raw = v.get("Rotation", "Auto (Horizontal)")
            if rot_raw in ["Horizontale", "Horizontal", "Auto (Horizontal)"]: rot_en = "Auto (Horizontal)"
            elif rot_raw in ["Toutes", "All", "Auto (All)"]: rot_en = "Auto (All)"
            elif rot_raw in ["Aucune", "None", "Strict: L -> Length"]: rot_en = "Strict: L -> Length"
            elif rot_raw == "Strict: W -> Length": rot_en = "Strict: W -> Length"
            else: rot_en = "Auto (Horizontal)"
            
            stack_raw = v.get("Stackable", v.get("Empilable", "Yes"))
            stack_en = "Yes" if stack_raw in ["Yes", "Oui"] else "No"
            
            lib_data.append({
                "Ref": v.get("Ref", k),
                "L (cm)": float(v.get("L", 120)),
                "W (cm)": float(v.get("W", v.get("l", 80))),
                "H (cm)": float(v.get("H", 100)),
                "Weight (kg)": float(v.get("Weight", v.get("Poids", 500))),
                "Rotation": rot_en,
                "Stackable": stack_en
            })
        
        if lib_data:
            lib_df = pd.DataFrame(lib_data)
            edited_lib = st.data_editor(
                lib_df, 
                num_rows="dynamic",
                column_config={
                    "Rotation": st.column_config.SelectboxColumn(options=["Auto (Horizontal)", "Auto (All)", "Strict: L -> Length", "Strict: W -> Length"]),
                    "Stackable": st.column_config.SelectboxColumn(options=["Yes", "No"])
                },
                key="lib_editor"
            )
            if st.button("💾 Save changes", use_container_width=True):
                new_lib = {}
                for row in edited_lib.to_dict('records'):
                    ref_key = str(row.get("Ref", "")).strip()
                    if ref_key and not pd.isna(ref_key) and ref_key != "nan":
                        new_lib[ref_key] = {
                            "Ref": ref_key,
                            "L": row["L (cm)"], "W": row["W (cm)"], "H": row["H (cm)"],
                            "Weight": row["Weight (kg)"],
                            "Rotation": row["Rotation"], "Stackable": row["Stackable"]
                        }
                st.session_state.product_lib = new_lib
                save_library(new_lib)
                st.success("Library updated successfully!")
                st.rerun()
        else:
            st.info("The library is empty.")

with col2:
    st.header("3. Cargo List (Editable)")
    
    if not st.session_state.cargo_items:
        st.write("The list is empty.")
        display_df = pd.DataFrame(columns=["Priority", "Reference", "Quantity", "Length", "Width", "Height", "Weight", "Rotation", "Stackable", "Color"])
    else:
        st.session_state.cargo_items = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priority", 999)))
        display_df = pd.DataFrame(st.session_state.cargo_items)
        
    edited_df = st.data_editor(
        display_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Priority": st.column_config.NumberColumn(min_value=1, step=1),
            "Quantity": st.column_config.NumberColumn(min_value=1, step=1),
            "Rotation": st.column_config.SelectboxColumn(options=["Auto (Horizontal)", "Auto (All)", "Strict: L -> Length", "Strict: W -> Length"]),
            "Stackable": st.column_config.SelectboxColumn(options=["Yes", "No"]),
            "Color": st.column_config.TextColumn("Color (Hex Code)")
        }
    )
    
    # Sync edited state back
    updated_items = edited_df.to_dict('records')
    st.session_state.cargo_items = updated_items
    for item in updated_items:
        if 'Color' in item and str(item.get('Reference', '')) != 'nan':
            st.session_state.color_map[item['Reference']] = item['Color']

    st.header("4. Optimization Result")
    if st.button("🚀 Calculate and display fleet", type="primary", use_container_width=True):
        if not st.session_state.cargo_items:
            st.warning("Please add cargo items.")
        else:
            impossible_items = set()
            for item in st.session_state.cargo_items:
                h = float(item["Height"])
                rot = item.get("Rotation", "Auto (Horizontal)")
                if rot in ["Auto (Horizontal)", "Strict: L -> Length", "Strict: W -> Length"] and h > c_props["H"]:
                    impossible_items.add(item["Reference"])
            
            if impossible_items:
                st.error(f"🚨 **DIMENSION ALERT** : Height of ({', '.join(impossible_items)}) exceeds the ceiling ({c_props['H']} cm). Check if you used millimeters.")
            
            with st.spinner("Calculating Optimization..."):
                sorted_cargo = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priority", 999)))
                all_items_to_pack = []
                for item in sorted_cargo:
                    rot_val = item.get("Rotation", "Auto (Horizontal)")
                    if rot_val in ["Auto (Horizontal)", "Horizontal", "Horizontale"]: allowed_rot = [0, 1]
                    elif rot_val in ["Auto (All)", "All", "Toutes"]: allowed_rot = [0, 1, 2, 3, 4, 5]
                    elif rot_val in ["Strict: L -> Length", "None", "Aucune"]: allowed_rot = [0]
                    elif rot_val == "Strict: W -> Length": allowed_rot = [1]
                    else: allowed_rot = [0, 1]
                    
                    is_stackable = item.get("Stackable", "Yes") == "Yes"
                    
                    h, weight = Decimal(str(item["Height"])), Decimal(str(item["Weight"]))
                    qty = int(item["Quantity"])
                    l_dec, w_dec = Decimal(str(item["Length"])), Decimal(str(item["Width"]))
                    ref_name = item["Reference"]

                    if is_stackable and rot_val in ["Auto (Horizontal)", "Strict: L -> Length", "Strict: W -> Length", "None", "Horizontal"]:
                        max_stack = int(Decimal(str(c_props["H"])) // h)
                        max_stack = max(1, min(max_stack, int(Decimal(str(c_props["max_weight"])) // weight) if weight > 0 else 999))
                    else:
                        max_stack = 1
                        
                    stack_count = 0
                    while qty > 0:
                        current_q = min(qty, max_stack)
                        stack_count += 1
                        c_item = CustomItem(f"{ref_name} #Stack{stack_count}", l_dec, w_dec, h * current_q, weight * current_q, allowed_rotations=allowed_rot, stackable=is_stackable)
                        c_item.original_qty, c_item.original_height, c_item.original_weight = current_q, h, weight
                        all_items_to_pack.append(c_item)
                        qty -= current_q

                unpacked_items, used_bins = list(all_items_to_pack), []

                for i in range(int(max_bins)):
                    if not unpacked_items: break 
                    bin_obj = Bin(f"{transport_type} #{i+1}", Decimal(str(c_props["L"])), Decimal(str(c_props["W"])), Decimal(str(c_props["H"])), Decimal(str(c_props["max_weight"])))
                    euro_slots = get_optimal_europallet_slots(transport_type)
                    
                    items_left = []
                    for item in unpacked_items:
                        if not pack_with_rules(bin_obj, item, euro_slots): items_left.append(item)
                    if len(bin_obj.items) > 0: used_bins.append(bin_obj)
                    unpacked_items = items_left

                final_used_bins = []
                for b in used_bins:
                    unpacked_items_list = []
                    for stack_item in b.items:
                        orig_q, base_name = getattr(stack_item, 'original_qty', 1), stack_item.name.split(" #Stack")[0]
                        if orig_q > 1:
                            orig_h, orig_w = getattr(stack_item, 'original_height'), getattr(stack_item, 'original_weight')
                            x, y, z = stack_item.position
                            for idx in range(orig_q):
                                single = CustomItem(f"{base_name} #{idx+1}", Decimal(str(stack_item.width)), Decimal(str(stack_item.height)), orig_h, orig_w, allowed_rotations=stack_item.allowed_rotations, stackable=stack_item.stackable)
                                single.rotation_type, single.position = stack_item.rotation_type, (Decimal(str(x)), Decimal(str(y)), Decimal(str(float(z) + idx * float(orig_h))))
                                unpacked_items_list.append(single)
                        else:
                            if " #Stack" in stack_item.name: stack_item.name = f"{base_name} #1"
                            unpacked_items_list.append(stack_item)
                    b.items = unpacked_items_list
                    final_used_bins.append(b)
                used_bins = final_used_bins

                # Unpacking refused items
                final_unfitted = []
                for stack_item in unpacked_items:
                    orig_q = getattr(stack_item, 'original_qty', 1)
                    base_name = stack_item.name.split(" #Stack")[0]
                    if orig_q > 1:
                        orig_h = getattr(stack_item, 'original_height')
                        orig_w = getattr(stack_item, 'original_weight')
                        for idx in range(orig_q):
                            single = CustomItem(f"{base_name} #{idx+1}", Decimal(str(stack_item.width)), Decimal(str(stack_item.height)), orig_h, orig_w, allowed_rotations=stack_item.allowed_rotations, stackable=stack_item.stackable)
                            final_unfitted.append(single)
                    else:
                        if " #Stack" in stack_item.name:
                            stack_item.name = f"{base_name} #1"
                        final_unfitted.append(stack_item)
                unpacked_items = final_unfitted

                # --- RENDER RESULTS ---
                st.success(f"✅ Optimization complete! {len(used_bins)} vehicle(s) used.")
                
                # Setup PDF Export
                pdf_path = generate_pdf_report(st.session_state.cargo_items, used_bins, c_props)
                if pdf_path:
                    with open(pdf_path, "rb") as pdf_file:
                        st.download_button(label="📄 Download PDF Report", data=pdf_file, file_name="cargo_report.pdf", mime="application/pdf")
                
                for b in used_bins:
                    total_vol = float(b.width * b.height * b.depth)
                    used_vol = sum([float(i.width * i.height * i.depth) for i in b.items])
                    fill_rate = (used_vol / total_vol) * 100 if total_vol > 0 else 0
                    st.markdown(f"### 🚛 {b.name} (Filled at {fill_rate:.1f}%)")
                    st.plotly_chart(plot_3d_packing((c_props["L"], c_props["W"], c_props["H"]), b.items, st.session_state.color_map, f"3D View - {b.name}"), use_container_width=True)
