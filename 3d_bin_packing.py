import streamlit as st
from py3dbp import Packer, Bin, Item
import plotly.graph_objects as go
import pandas as pd
from decimal import Decimal
import json
import firebase_admin
from firebase_admin import credentials, firestore

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="3D Cargo Optimizer", layout="wide")

# --- FIREBASE CONNECTION ---
# Check if Firebase is already initialized (prevents errors on page reload)
try:
    if not firebase_admin._apps:
        # Retrieve secret key from Streamlit Secrets
        key_dict = json.loads(st.secrets["firebase_credentials"])
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)
    
    # Connect to Firestore
    db = firestore.client()
    FIREBASE_ENABLED = True
except Exception as e:
    st.error(f"⚠️ Firebase configuration missing or invalid. Please check your Streamlit Secrets. Error: {e}")
    FIREBASE_ENABLED = False

def load_library():
    """Load all products from the Firestore 'products' collection"""
    if not FIREBASE_ENABLED:
        return {}
    try:
        docs = db.collection(u'products').stream()
        lib = {}
        for doc in docs:
            lib[doc.id] = doc.to_dict()
        return lib
    except Exception as e:
        st.error(f"Database connection error: {e}")
        return {}

def save_library(library_data):
    """Save each product into the Firestore 'products' collection"""
    if not FIREBASE_ENABLED:
        return
    try:
        for key, value in library_data.items():
            db.collection(u'products').document(key).set(value)
    except Exception as e:
        st.error(f"Save error: {e}")

# Load library into memory from Firebase
if 'product_lib' not in st.session_state:
    st.session_state.product_lib = load_library()

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

# --- CUSTOM CLASSES ---
class CustomItem(Item):
    def __init__(self, name, width, height, depth, weight, allowed_rotations, stackable):
        super().__init__(name, width, height, depth, weight)
        self.allowed_rotations = allowed_rotations 
        self.stackable = stackable                 

    def get_dimension(self):
        current_rotation = getattr(self, 'rotation_type', 0)
        if current_rotation not in self.allowed_rotations:
            huge = Decimal('999999')
            return (huge, huge, huge)
        return super().get_dimension()

# --- ADVANCED PLACEMENT ENGINE (EXTREME POINT BEST FIT) ---
def custom_pack_item_to_bin(bin_obj, item):
    """
    Advanced placement algorithm: evaluates all pivot points and rotations
    to maximize contact surface (Best-Fit) for various dimensions.
    """
    pivots = set()
    pivots.add((Decimal('0'), Decimal('0'), Decimal('0')))
    for p in bin_obj.items:
        w, h, d = p.get_dimension()
        px, py, pz = p.position
        
        pivots.add((px + w, py, pz))
        pivots.add((px, py + h, pz))
        pivots.add((px, py, pz + d))
        
        for q in bin_obj.items:
            if p != q:
                qw, qh, qd = q.get_dimension()
                qx, qy, qz = q.position
                pivots.add((px + w, qy, pz))
                pivots.add((px, qy + qh, pz))
                pivots.add((qx, py + h, pz))
                pivots.add((qx + qw, py + h, pz))

    valid_pivots = [p for p in pivots if p[0] < bin_obj.width and p[1] < bin_obj.height and p[2] < bin_obj.depth]
    sorted_pivots = sorted(valid_pivots, key=lambda p: (p[2], p[0], p[1]))

    best_score = -9999999
    best_pivot = None
    best_rot = None

    for pivot in sorted_pivots:
        for rot in item.allowed_rotations:
            item.rotation_type = rot
            dim = item.get_dimension()
            w, h, d = dim[0], dim[1], dim[2]
            
            if bin_obj.width < pivot[0] + w or bin_obj.height < pivot[1] + h or bin_obj.depth < pivot[2] + d:
                continue
                
            current_weight = sum(i.weight for i in bin_obj.items)
            if current_weight + item.weight > bin_obj.max_weight:
                continue

            collision = False
            for p_item in bin_obj.items:
                pw, ph, pd = p_item.get_dimension()
                px, py, pz = p_item.position
                if not (pivot[0] >= px + pw or pivot[0] + w <= px or
                        pivot[1] >= py + ph or pivot[1] + h <= py or
                        pivot[2] >= pz + pd or pivot[2] + d <= pz):
                    collision = True
                    break
                    
                if getattr(p_item, 'stackable', True) is False:
                    if pivot[2] >= pz + pd:
                        if not (pivot[0] >= px + pw or pivot[0] + w <= px or
                                pivot[1] >= py + ph or pivot[1] + h <= py):
                            collision = True
                            break
                            
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
                    
                    if pivot[0] == px + pw or pivot[0] + w == px:
                        oy = min(pivot[1]+h, py+ph) - max(pivot[1], py)
                        oz = min(pivot[2]+d, pz+pd) - max(pivot[2], pz)
                        if oy > 0 and oz > 0: score += float(oy * oz) * 2
                            
                    if pivot[1] == py + ph or pivot[1] + h == py:
                        ox = min(pivot[0]+w, px+pw) - max(pivot[0], px)
                        oz = min(pivot[2]+d, pz+pd) - max(pivot[2], pz)
                        if ox > 0 and oz > 0: score += float(ox * oz) * 2
                            
                    if pivot[2] == pz + pd or pivot[2] + d == pz:
                        ox = min(pivot[0]+w, px+pw) - max(pivot[0], px)
                        oy = min(pivot[1]+h, py+ph) - max(pivot[1], py)
                        if ox > 0 and oy > 0: score += float(ox * oy) * 2

                score -= (float(pivot[0]) + float(pivot[1]) + float(pivot[2])) * 0.1

                if score > best_score:
                    best_score = score
                    best_pivot = pivot
                    best_rot = rot
                    
    if best_pivot is not None:
        item.rotation_type = best_rot
        item.position = best_pivot
        bin_obj.items.append(item)
        return True
    return False

# --- STRICT LOGISTICS RULES (EUROPALLETS) ---
def get_optimal_europallet_slots(container_name):
    """Generates exact coordinates (hardcoded) for standard industrial optimization patterns."""
    slots = []
    if "20FT" in container_name:
        # Pinwheel configuration 11 Pallets (7 vertical, 4 horizontal)
        for i in range(7): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(4): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "40FT" in container_name or "40HQ" in container_name:
        # Pinwheel configuration 25 Pallets (15 vertical, 10 horizontal)
        for i in range(15): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(10): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "TIR" in container_name:
        # Configuration 33 Pallets (11 rows of 3)
        for i in range(11):
            for j in range(3):
                slots.append({'x': i*120, 'y': j*80, 'w': 120, 'h': 80, 'filled': False})
    return slots

def pack_with_rules(bin_obj, item, euro_slots):
    """Attempts to apply strict Europallet rules, otherwise falls back to the dynamic algorithm."""
    l, w = float(item.width), float(item.height)
    is_europallet = (abs(l - 120) <= 1 and abs(w - 80) <= 1) or (abs(l - 80) <= 1 and abs(w - 120) <= 1)
    
    if is_europallet:
        for slot in euro_slots:
            if not slot['filled']:
                current_weight = sum(i.weight for i in bin_obj.items)
                if current_weight + item.weight > bin_obj.max_weight:
                    continue 
                if float(item.depth) > float(bin_obj.depth): 
                    continue
                    
                assigned = False
                for rot in item.allowed_rotations:
                    item.rotation_type = rot
                    d = item.get_dimension()
                    if abs(float(d[0]) - slot['w']) <= 1 and abs(float(d[1]) - slot['h']) <= 1:
                        # Forced positioning to the exact centimeter according to industrial rule
                        item.position = (Decimal(str(slot['x'])), Decimal(str(slot['y'])), Decimal('0'))
                        bin_obj.items.append(item)
                        assigned = True
                        slot['filled'] = True
                        break
                if assigned:
                    return True
                    
    # If not a Europallet or if perfect slots are full -> Dynamic Algorithm
    return custom_pack_item_to_bin(bin_obj, item)

# --- UTILITY FUNCTIONS ---
def plot_3d_packing(container_dim, fitted_items, color_map, title):
    """Generates 3D visualization for a given container."""
    fig = go.Figure()
    cx, cy, cz = container_dim

    # Container contours
    x_lines = [0, cx, cx, 0, 0, None, 0, cx, cx, 0, 0, None, 0, 0, None, cx, cx, None, cx, cx, None, 0, 0]
    y_lines = [0, 0, cy, cy, 0, None, 0, 0, cy, cy, 0, None, 0, 0, None, 0, 0, None, cy, cy, None, cy, cy]
    z_lines = [0, 0, 0, 0, 0, None, cz, cz, cz, cz, cz, None, 0, cz, None, 0, cz, None, 0, cz, None, 0, cz]
    
    fig.add_trace(go.Scatter3d(
        x=x_lines, y=y_lines, z=z_lines,
        mode='lines', line=dict(color='gray', width=3),
        name="Container", hoverinfo='skip'
    ))

    all_x_edges, all_y_edges, all_z_edges = [], [], []

    for item in fitted_items:
        ref_name = item.name.split(" #")[0]
        color = color_map.get(ref_name, '#333333')
        
        # Explicit float conversion to avoid Decimal + float errors
        x, y, z = map(float, item.position)
        w, h, d = map(float, item.get_dimension())

        x_coords = [x, x+w, x+w, x,   x, x+w, x+w, x]
        y_coords = [y, y,   y+h, y+h, y, y,   y+h, y+h]
        z_coords = [z, z,   z,   z,   z+d, z+d, z+d, z+d]

        i_faces = [0, 0, 4, 4, 0, 0, 3, 3, 0, 0, 1, 1]
        j_faces = [1, 2, 5, 6, 1, 5, 2, 6, 3, 7, 2, 6]
        k_faces = [2, 3, 6, 7, 5, 4, 6, 7, 7, 4, 6, 5]

        emp_status = "✅ Yes" if getattr(item, 'stackable', True) else "❌ NO"
        hovertext = f"<b>{item.name}</b><br>Dim : {w}x{h}x{d} cm<br>Stackable : {emp_status}"

        fig.add_trace(go.Mesh3d(
            x=x_coords, y=y_coords, z=z_coords,
            i=i_faces, j=j_faces, k=k_faces,
            color=color, opacity=1.0, flatshading=True,
            name=ref_name, hoverinfo="text", text=hovertext, showscale=False
        ))

        all_x_edges.extend([x, x+w, x+w, x, x, None, x, x+w, x+w, x, x, None, x, x, None, x+w, x+w, None, x+w, x+w, None, x, x, None])
        all_y_edges.extend([y, y, y+h, y+h, y, None, y, y, y+h, y+h, y, None, y, y, None, y, y, None, y+h, y+h, None, y+h, y+h, None])
        all_z_edges.extend([z, z, z, z, z, None, z+d, z+d, z+d, z+d, z+d, None, z, z+d, None, z, z+d, None, z, z+d, None, z, z+d, None])

    if all_x_edges:
        fig.add_trace(go.Scatter3d(
            x=all_x_edges, y=all_y_edges, z=all_z_edges,
            mode='lines', line=dict(color='black', width=3),
            hoverinfo='skip', showlegend=False
        ))

    fig.update_layout(
        title=title,
        scene=dict(
            xaxis=dict(title='Length (cm) [Front -> Back]', range=[0, cx * 1.1]),
            yaxis=dict(title='Width (cm)', range=[0, cy * 1.1]),
            zaxis=dict(title='Height (cm)', range=[0, cz * 1.1]),
            aspectmode='data', camera=dict(eye=dict(x=1.5, y=1.5, z=1.5))
        ),
        margin=dict(l=0, r=0, b=0, t=40),
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
    )
    return fig

# --- USER INTERFACE ---
st.title("📦 3D Multi-Vehicle Cargo Optimizer")

if 'cargo_items' not in st.session_state:
    st.session_state.cargo_items = []
if 'color_map' not in st.session_state:
    st.session_state.color_map = {}

col1, col2 = st.columns([1, 2])

with col1:
    st.header("1. Transport Parameters")
    transport_type = st.selectbox("Container / Truck Type", list(CONTAINERS.keys()))
    c_props = CONTAINERS[transport_type]
    max_bins = st.number_input("Maximum number of vehicles in fleet", min_value=1, max_value=10, value=2, 
                               help="If the cargo exceeds one vehicle's capacity, the algorithm will automatically fill the next one(s).")
    st.info(f"Dimensions : {c_props['L']} x {c_props['W']} x {c_props['H']} cm | Max weight : {c_props['max_weight']} kg")

    st.header("2. Add Cargo")
    
    # Library Selector
    lib_keys = list(st.session_state.product_lib.keys())
    selected_preset = st.selectbox("📚 Load a product from the library", ["-- New product --"] + lib_keys)
    
    # Default values based on selection
    d_ref, d_l, d_w, d_h, d_weight = "", 120.0, 80.0, 100.0, 500.0
    d_rot, d_stack = 1, 0
    if selected_preset != "-- New product --":
        prod = st.session_state.product_lib[selected_preset]
        d_ref, d_l, d_w, d_h, d_weight = prod["Ref"], float(prod["L"]), float(prod["W"]), float(prod["H"]), float(prod["Weight"])
        rot_mapping = {"None": 0, "Horizontal": 1, "All": 2}
        d_rot = rot_mapping.get(prod.get("Rotation", "Horizontal"), 1)
        d_stack = 0 if prod.get("Stackable", "Yes") == "Yes" else 1

    with st.form("add_item_form", clear_on_submit=False):
        ref = st.text_input("Reference", value=d_ref)
        
        col_qty, col_prio = st.columns(2)
        qty = col_qty.number_input("Quantity", min_value=1, value=1, step=1)
        prio = col_prio.number_input("Priority (1 = First)", min_value=1, value=len(st.session_state.cargo_items)+1, step=1)
        
        st.markdown("**Warning, enter values in CENTIMETERS!**")
        col_l, col_w, col_h = st.columns(3)
        l = col_l.number_input("Length (cm)", min_value=1.0, value=d_l, step=1.0)
        w = col_w.number_input("Width (cm)", min_value=1.0, value=d_w, step=1.0)
        h = col_h.number_input("Height (cm)", min_value=1.0, value=d_h, step=1.0)
        
        weight = st.number_input("Unit Weight (kg)", min_value=0.1, value=d_weight, step=10.0)
        
        rotation_policy = st.radio(
            "Allowed Rotation",
            options=["None", "Horizontal", "All"],
            index=d_rot, horizontal=True
        )
        stackable = st.radio(
            "Stackable Product?",
            options=["Yes", "No"],
            index=d_stack, horizontal=True
        )
        
        save_to_lib = st.checkbox("💾 Save this product to the library for future sessions", value=False)
        
        submit = st.form_submit_button("Add to list")

        if submit:
            if ref:
                # Add to active list with Priority
                st.session_state.cargo_items.append({
                    "Priority": int(prio),
                    "Reference": ref, "Quantity": qty, 
                    "Length": l, "Width": w, "Height": h, "Weight": weight,
                    "Rotation": rotation_policy, "Stackable": stackable
                })
                
                # Assign color
                if ref not in st.session_state.color_map:
                    st.session_state.color_map[ref] = DISTINCT_COLORS[len(st.session_state.color_map) % len(DISTINCT_COLORS)]
                
                # Save to library if requested
                if save_to_lib:
                    st.session_state.product_lib[ref] = {
                        "Ref": ref, "L": l, "W": w, "H": h, "Weight": weight, 
                        "Rotation": rotation_policy, "Stackable": stackable
                    }
                    save_library(st.session_state.product_lib)
                    st.success(f"'{ref}' saved in the library!")
                else:
                    st.success(f"Added : {qty}x {ref} (Priority {prio})")
            else:
                st.error("Please enter a reference.")

    # --- LIBRARY EDITOR ---
    st.markdown("---")
    with st.expander("⚙️ Library Editor (Edit / Delete products)"):
        lib_data = []
        for k, v in st.session_state.product_lib.items():
            lib_data.append({
                "Ref": v.get("Ref", k),
                "L (cm)": float(v.get("L", 120)),
                "W (cm)": float(v.get("W", 80)),
                "H (cm)": float(v.get("H", 100)),
                "Weight (kg)": float(v.get("Weight", 500)),
                "Rotation": v.get("Rotation", "Horizontal"),
                "Stackable": v.get("Stackable", "Yes")
            })
        
        if lib_data:
            lib_df = pd.DataFrame(lib_data)
            edited_lib = st.data_editor(
                lib_df, 
                num_rows="dynamic",
                column_config={
                    "Rotation": st.column_config.SelectboxColumn(options=["None", "Horizontal", "All"]),
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
    st.info("💡 The algorithm will load pallets **strictly in priority order**. Change numbers in the Priority column to adjust the loading order.")
    
    if not st.session_state.cargo_items:
        st.write("The list is empty.")
        display_df = pd.DataFrame(columns=["Priority", "Reference", "Quantity", "Length", "Width", "Height", "Weight", "Rotation", "Stackable"])
    else:
        # Automatic sorting of list by priority before displaying
        st.session_state.cargo_items = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priority", 999)))
        display_df = pd.DataFrame(st.session_state.cargo_items)
        
    # --- EDITABLE TABLE ---
    edited_df = st.data_editor(
        display_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Priority": st.column_config.NumberColumn(min_value=1, step=1),
            "Quantity": st.column_config.NumberColumn(min_value=1, step=1),
            "Rotation": st.column_config.SelectboxColumn(options=["None", "Horizontal", "All"]),
            "Stackable": st.column_config.SelectboxColumn(options=["Yes", "No"])
        }
    )
    # Update state with manual modifications made in the table
    st.session_state.cargo_items = edited_df.to_dict('records')

    st.header("4. Optimization Result")
    if st.button("🚀 Calculate and display fleet", type="primary", use_container_width=True):
        if not st.session_state.cargo_items:
            st.warning("Please add cargo items.")
        else:
            # --- PRE-CALCULATION VERIFICATION (SANITY CHECK MILLIMETERS) ---
            impossible_items = set()
            for item in st.session_state.cargo_items:
                h = float(item["Height"])
                rot = item.get("Rotation", "Horizontal")
                if rot in ["None", "Horizontal"] and h > c_props["H"]:
                    impossible_items.add(item["Reference"])
            
            if impossible_items:
                st.error(f"🚨 **DIMENSION ALERT** : The height of the following items ({', '.join(impossible_items)}) exceeds the truck's ceiling ({c_props['H']} cm). \n\n👉 **Did you enter millimeters (e.g. 1060) instead of centimeters (e.g. 106)?** The algorithm will refuse to load them.")
            
            with st.spinner("Calculating (Business Rules + Heuristics)..."):
                
                # Ensure the table is properly sorted
                sorted_cargo = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priority", 999)))
                
                # 1. Prepare absolute list of ALL boxes by PRE-STACKING pallets
                all_items_to_pack = []
                for item in sorted_cargo:
                    rot_val = item.get("Rotation", "Horizontal")
                    if rot_val == "None": allowed_rot = [0]
                    elif rot_val == "Horizontal": allowed_rot = [0, 1]
                    else: allowed_rot = [0, 1, 2, 3, 4, 5]
                    
                    is_stackable = True if item.get("Stackable", "Yes") == "Yes" else False
                    
                    # Force Decimal type to avoid internal TypeError
                    h = Decimal(str(item["Height"]))
                    weight = Decimal(str(item["Weight"]))
                    qty = int(item["Quantity"])
                    l_dec = Decimal(str(item["Length"]))
                    w_dec = Decimal(str(item["Width"]))
                    
                    ref_name = item["Reference"]
                    if ref_name not in st.session_state.color_map:
                        st.session_state.color_map[ref_name] = DISTINCT_COLORS[len(st.session_state.color_map) % len(DISTINCT_COLORS)]

                    # PRE-STACKING (Virtual Stacking)
                    if is_stackable and rot_val in ["None", "Horizontal"]:
                        max_stack = int(Decimal(str(c_props["H"])) // h)
                        max_stack = max(1, min(max_stack, int(Decimal(str(c_props["max_weight"])) // weight) if weight > 0 else 999))
                    else:
                        max_stack = 1
                        
                    stack_count = 0
                    while qty > 0:
                        current_q = min(qty, max_stack)
                        stack_count += 1
                        item_name = f"{ref_name} #Stack{stack_count}"
                        
                        c_item = CustomItem(
                            item_name, 
                            l_dec, 
                            w_dec, 
                            h * current_q, 
                            weight * current_q,
                            allowed_rotations=allowed_rot, 
                            stackable=is_stackable
                        )
                        c_item.original_qty = current_q
                        c_item.original_height = h
                        c_item.original_weight = weight
                        
                        all_items_to_pack.append(c_item)
                        qty -= current_q

                unpacked_items = list(all_items_to_pack)
                used_bins = []

                # 2. Manual truck by truck loading
                for i in range(int(max_bins)):
                    if not unpacked_items:
                        break # Everything is loaded!
                    
                    # Force Decimal type for container dimensions and max weight
                    bin_obj = Bin(
                        f"{transport_type} #{i+1}", 
                        Decimal(str(c_props["L"])), 
                        Decimal(str(c_props["W"])), 
                        Decimal(str(c_props["H"])), 
                        Decimal(str(c_props["max_weight"]))
                    )
                    
                    # Generate perfect slots for Europallets in THIS truck
                    euro_slots = get_optimal_europallet_slots(transport_type)
                    
                    items_left = []
                    # Insertion respecting priority order, Europallet rules, AND optimization
                    for item in unpacked_items:
                        success = pack_with_rules(bin_obj, item, euro_slots)
                        if not success:
                            items_left.append(item)
                    
                    if len(bin_obj.items) > 0:
                        used_bins.append(bin_obj)
                    
                    # Transfer unfitted items to the next truck
                    unpacked_items = items_left

                # 3. UNPACKING VIRTUAL COLUMNS FOR VISUALIZATION
                final_used_bins = []
                for b in used_bins:
                    unpacked_items_list = []
                    for stack_item in b.items:
                        orig_q = getattr(stack_item, 'original_qty', 1)
                        base_name = stack_item.name.split(" #Stack")[0]
                        
                        if orig_q > 1:
                            orig_h = getattr(stack_item, 'original_height')
                            orig_w = getattr(stack_item, 'original_weight')
                            x, y, z = stack_item.position
                            
                            for idx in range(orig_q):
                                single = CustomItem(
                                    f"{base_name} #{idx+1}", 
                                    Decimal(str(stack_item.width)), 
                                    Decimal(str(stack_item.height)), 
                                    orig_h, 
                                    orig_w,
                                    allowed_rotations=stack_item.allowed_rotations,
                                    stackable=stack_item.stackable
                                )
                                single.rotation_type = stack_item.rotation_type
                                single.position = (Decimal(str(x)), Decimal(str(y)), Decimal(str(float(z) + idx * float(orig_h))))
                                unpacked_items_list.append(single)
                        else:
                            if " #Stack" in stack_item.name:
                                stack_item.name = f"{base_name} #1"
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
                            single = CustomItem(
                                f"{base_name} #{idx+1}", 
                                Decimal(str(stack_item.width)), 
                                Decimal(str(stack_item.height)), 
                                orig_h, 
                                orig_w,
                                allowed_rotations=stack_item.allowed_rotations, 
                                stackable=stack_item.stackable
                            )
                            final_unfitted.append(single)
                    else:
                        if " #Stack" in stack_item.name:
                            stack_item.name = f"{base_name} #1"
                        final_unfitted.append(stack_item)
                unpacked_items = final_unfitted

                # --- DISPLAY RESULTS ---
                st.success(f"✅ Optimization complete! {len(used_bins)} vehicle(s) used.")
                
                if len(unpacked_items) > 0:
                    st.error(f"❌ The fleet of {max_bins} vehicle(s) is full (or some pallets are too large)! {len(unpacked_items)} item(s) left behind.")
                
                for b in used_bins:
                    total_vol = float(b.width * b.height * b.depth)
                    used_vol = sum([float(i.width * i.height * i.depth) for i in b.items])
                    fill_rate = (used_vol / total_vol) * 100 if total_vol > 0 else 0
                    
                    st.markdown(f"### 🚛 {b.name} (Filled at {fill_rate:.1f}%)")
                    st.caption(f"{len(b.items)} items loaded in this vehicle.")
                    
                    fig = plot_3d_packing(
                        (c_props["L"], c_props["W"], c_props["H"]), 
                        b.items, 
                        st.session_state.color_map,
                        title=f"3D View - {b.name}"
                    )
                    st.plotly_chart(fig, use_container_width=True)
