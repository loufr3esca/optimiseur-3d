import streamlit as st
from py3dbp import Packer, Bin, Item
import plotly.graph_objects as go
import pandas as pd
from decimal import Decimal
import json
import os

# --- NOUVEAU MOTEUR DE PLACEMENT (EXTREME POINT BEST FIT) ---
def custom_pack_item_to_bin(bin_obj, item):
    """
    Algorithme de placement avancé : évalue tous les points de pivot et rotations
    pour maximiser la surface de contact (Best-Fit) des colis de dimensions variées.
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

# --- RÈGLES LOGISTIQUES FIXES (EUROPALLETS) ---
def get_optimal_europallet_slots(container_name):
    """Génère les coordonnées exactes (en dur) des schémas d'optimisation industriels."""
    slots = []
    if "20FT" in container_name:
        # Configuration Pinwheel 11 Palettes (7 verticales, 4 horizontales)
        for i in range(7): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(4): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "40FT" in container_name or "40HQ" in container_name:
        # Configuration Pinwheel 25 Palettes (15 verticales, 10 horizontales)
        for i in range(15): slots.append({'x': i*80, 'y': 0, 'w': 80, 'h': 120, 'filled': False})
        for i in range(10): slots.append({'x': i*120, 'y': 120, 'w': 120, 'h': 80, 'filled': False})
    elif "TIR" in container_name:
        # Configuration 33 Palettes (11 rangées de 3)
        for i in range(11):
            for j in range(3):
                slots.append({'x': i*120, 'y': j*80, 'w': 120, 'h': 80, 'filled': False})
    return slots

def pack_with_rules(bin_obj, item, euro_slots):
    """Tente d'appliquer les règles strictes d'Europalette, sinon utilise l'algorithme générique."""
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
                        # Positionnement forcé au centimètre près selon la règle industrielle
                        item.position = (Decimal(str(slot['x'])), Decimal(str(slot['y'])), Decimal('0'))
                        bin_obj.items.append(item)
                        assigned = True
                        slot['filled'] = True
                        break
                if assigned:
                    return True
                    
    # Si ce n'est pas une Europalette ou si les slots parfaits sont pleins -> Algorithme dynamique
    return custom_pack_item_to_bin(bin_obj, item)


# --- CONFIGURATION DE LA PAGE ---
st.set_page_config(page_title="Optimiseur de Chargement 3D", layout="wide")

# --- FICHIER DE BIBLIOTHÈQUE ---
LIBRARY_FILE = "products_library.json"

def load_library():
    if os.path.exists(LIBRARY_FILE):
        try:
            with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_library(library_data):
    with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump(library_data, f, indent=4, ensure_ascii=False)

# Chargement de la bibliothèque en mémoire
if 'product_lib' not in st.session_state:
    st.session_state.product_lib = load_library()

# --- DONNÉES DES CONTENEURS ---
CONTAINERS = {
    "TIR (Semi-remorque)": {"L": 1360, "l": 245, "h": 270, "poids_max": 24000},
    "20FT Standard": {"L": 589, "l": 235, "h": 239, "poids_max": 28000},
    "40FT Standard": {"L": 1203, "l": 235, "h": 239, "poids_max": 28000},
    "40HQ (High Cube)": {"L": 1203, "l": 235, "h": 269, "poids_max": 28000}
}

DISTINCT_COLORS = [
    '#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', 
    '#911eb4', '#46f0f0', '#f032e6', '#bcf60c', '#fabebe', 
    '#008080', '#e6beff', '#9a6324', '#fffac8', '#800000'
]

# --- CLASSES PERSONNALISÉES ---
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

# --- FONCTIONS UTILITAIRES ---
def plot_3d_packing(container_dim, fitted_items, color_map, title):
    """Génère la visualisation 3D pour un conteneur donné."""
    fig = go.Figure()
    cx, cy, cz = container_dim

    # Contours du conteneur
    x_lines = [0, cx, cx, 0, 0, None, 0, cx, cx, 0, 0, None, 0, 0, None, cx, cx, None, cx, cx, None, 0, 0]
    y_lines = [0, 0, cy, cy, 0, None, 0, 0, cy, cy, 0, None, 0, 0, None, 0, 0, None, cy, cy, None, cy, cy]
    z_lines = [0, 0, 0, 0, 0, None, cz, cz, cz, cz, cz, None, 0, cz, None, 0, cz, None, 0, cz, None, 0, cz]
    
    fig.add_trace(go.Scatter3d(
        x=x_lines, y=y_lines, z=z_lines,
        mode='lines', line=dict(color='gray', width=3),
        name="Conteneur", hoverinfo='skip'
    ))

    all_x_edges, all_y_edges, all_z_edges = [], [], []

    for item in fitted_items:
        ref_name = item.name.split(" #")[0]
        color = color_map.get(ref_name, '#333333')
        
        # Conversion explicite en float pour éviter l'erreur Decimal + float
        x, y, z = map(float, item.position)
        w, h, d = map(float, item.get_dimension())

        x_coords = [x, x+w, x+w, x,   x, x+w, x+w, x]
        y_coords = [y, y,   y+h, y+h, y, y,   y+h, y+h]
        z_coords = [z, z,   z,   z,   z+d, z+d, z+d, z+d]

        i_faces = [0, 0, 4, 4, 0, 0, 3, 3, 0, 0, 1, 1]
        j_faces = [1, 2, 5, 6, 1, 5, 2, 6, 3, 7, 2, 6]
        k_faces = [2, 3, 6, 7, 5, 4, 6, 7, 7, 4, 6, 5]

        emp_status = "✅ Oui" if getattr(item, 'stackable', True) else "❌ NON"
        hovertext = f"<b>{item.name}</b><br>Dim : {w}x{h}x{d} cm<br>Empilable : {emp_status}"

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
            xaxis=dict(title='Longueur (cm) [Avant -> Arrière]', range=[0, cx * 1.1]),
            yaxis=dict(title='Largeur (cm)', range=[0, cy * 1.1]),
            zaxis=dict(title='Hauteur (cm)', range=[0, cz * 1.1]),
            aspectmode='data', camera=dict(eye=dict(x=1.5, y=1.5, z=1.5))
        ),
        margin=dict(l=0, r=0, b=0, t=40),
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01)
    )
    return fig

# --- INTERFACE UTILISATEUR ---
st.title("📦 Optimisation de Chargement 3D Multi-Véhicules")

if 'cargo_items' not in st.session_state:
    st.session_state.cargo_items = []
if 'color_map' not in st.session_state:
    st.session_state.color_map = {}

col1, col2 = st.columns([1, 2])

with col1:
    st.header("1. Paramètres de transport")
    transport_type = st.selectbox("Type de conteneur / camion", list(CONTAINERS.keys()))
    c_props = CONTAINERS[transport_type]
    max_bins = st.number_input("Nombre maximum de véhicules dans la flotte", min_value=1, max_value=10, value=2, 
                               help="Si le chargement dépasse la capacité d'un véhicule, l'algorithme remplira automatiquement le(s) suivant(s).")
    st.info(f"Dimensions : {c_props['L']} x {c_props['l']} x {c_props['h']} cm | Poids max : {c_props['poids_max']} kg")

    st.header("2. Ajouter de la marchandise")
    
    # Sélecteur de bibliothèque
    lib_keys = list(st.session_state.product_lib.keys())
    selected_preset = st.selectbox("📚 Charger un produit depuis la bibliothèque", ["-- Nouveau produit --"] + lib_keys)
    
    # Valeurs par défaut basées sur la sélection
    d_ref, d_l, d_w, d_h, d_weight = "", 120.0, 80.0, 100.0, 500.0
    d_rot, d_stack = 1, 0
    if selected_preset != "-- Nouveau produit --":
        prod = st.session_state.product_lib[selected_preset]
        d_ref, d_l, d_w, d_h, d_weight = prod["Ref"], float(prod["L"]), float(prod["l"]), float(prod["H"]), float(prod["Poids"])
        rot_mapping = {"Aucune": 0, "Horizontale": 1, "Toutes": 2}
        d_rot = rot_mapping.get(prod.get("Rotation", "Horizontale"), 1)
        d_stack = 0 if prod.get("Empilable", "Oui") == "Oui" else 1

    with st.form("add_item_form", clear_on_submit=False):
        ref = st.text_input("Référence", value=d_ref)
        
        col_qty, col_prio = st.columns(2)
        qty = col_qty.number_input("Quantité", min_value=1, value=1, step=1)
        prio = col_prio.number_input("Priorité (1 = Premier)", min_value=1, value=len(st.session_state.cargo_items)+1, step=1)
        
        st.markdown("**Attention, saisissez les valeurs en CENTIMÈTRES !**")
        col_l, col_w, col_h = st.columns(3)
        l = col_l.number_input("Longueur (cm)", min_value=1.0, value=d_l, step=1.0)
        w = col_w.number_input("Largeur (cm)", min_value=1.0, value=d_w, step=1.0)
        h = col_h.number_input("Hauteur (cm)", min_value=1.0, value=d_h, step=1.0)
        
        weight = st.number_input("Poids unitaire (kg)", min_value=0.1, value=d_weight, step=10.0)
        
        rotation_policy = st.radio(
            "Rotation autorisée",
            options=["Aucune", "Horizontale", "Toutes"],
            index=d_rot, horizontal=True
        )
        stackable = st.radio(
            "Produit empilable ?",
            options=["Oui", "Non"],
            index=d_stack, horizontal=True
        )
        
        save_to_lib = st.checkbox("💾 Sauvegarder ce produit dans la bibliothèque pour de futures sessions", value=False)
        
        submit = st.form_submit_button("Ajouter à la liste")

        if submit:
            if ref:
                # Ajout à la liste active avec Priorité
                st.session_state.cargo_items.append({
                    "Priorité": int(prio),
                    "Référence": ref, "Quantité": qty, 
                    "Longueur": l, "Largeur": w, "Hauteur": h, "Poids": weight,
                    "Rotation": rotation_policy, "Empilable": stackable
                })
                
                # Assignation couleur
                if ref not in st.session_state.color_map:
                    st.session_state.color_map[ref] = DISTINCT_COLORS[len(st.session_state.color_map) % len(DISTINCT_COLORS)]
                
                # Sauvegarde en bibliothèque si demandé
                if save_to_lib:
                    st.session_state.product_lib[ref] = {
                        "Ref": ref, "L": l, "l": w, "H": h, "Poids": weight, 
                        "Rotation": rotation_policy, "Empilable": stackable
                    }
                    save_library(st.session_state.product_lib)
                    st.success(f"'{ref}' enregistré dans la bibliothèque !")
                else:
                    st.success(f"Ajouté : {qty}x {ref} (Priorité {prio})")
            else:
                st.error("Veuillez entrer une référence.")

    # --- ÉDITEUR DE BIBLIOTHÈQUE ---
    st.markdown("---")
    with st.expander("⚙️ Éditeur de Bibliothèque (Modifier / Supprimer des produits)"):
        lib_data = []
        for k, v in st.session_state.product_lib.items():
            lib_data.append({
                "Ref": v.get("Ref", k),
                "L (cm)": float(v.get("L", 120)),
                "l (cm)": float(v.get("l", 80)),
                "H (cm)": float(v.get("H", 100)),
                "Poids (kg)": float(v.get("Poids", 500)),
                "Rotation": v.get("Rotation", "Horizontale"),
                "Empilable": v.get("Empilable", "Oui")
            })
        
        if lib_data:
            lib_df = pd.DataFrame(lib_data)
            edited_lib = st.data_editor(
                lib_df, 
                num_rows="dynamic",
                column_config={
                    "Rotation": st.column_config.SelectboxColumn(options=["Aucune", "Horizontale", "Toutes"]),
                    "Empilable": st.column_config.SelectboxColumn(options=["Oui", "Non"])
                },
                key="lib_editor"
            )
            if st.button("💾 Enregistrer les modifications", use_container_width=True):
                new_lib = {}
                for row in edited_lib.to_dict('records'):
                    ref_key = str(row.get("Ref", "")).strip()
                    if ref_key and not pd.isna(ref_key) and ref_key != "nan":
                        new_lib[ref_key] = {
                            "Ref": ref_key,
                            "L": row["L (cm)"], "l": row["l (cm)"], "H": row["H (cm)"],
                            "Poids": row["Poids (kg)"],
                            "Rotation": row["Rotation"], "Empilable": row["Empilable"]
                        }
                st.session_state.product_lib = new_lib
                save_library(new_lib)
                st.success("Bibliothèque mise à jour avec succès !")
                st.rerun()
        else:
            st.info("La bibliothèque est vide.")

with col2:
    st.header("3. Liste des marchandises (Éditable)")
    st.info("💡 L'algorithme chargera les palettes **strictement dans l'ordre de priorité**. Changez les numéros de la colonne **Priorité** pour ajuster l'ordre de remplissage.")
    
    if not st.session_state.cargo_items:
        st.write("La liste est vide.")
        display_df = pd.DataFrame(columns=["Priorité", "Référence", "Quantité", "Longueur", "Largeur", "Hauteur", "Poids", "Rotation", "Empilable"])
    else:
        # Tri automatique de la liste par priorité avant de l'afficher
        st.session_state.cargo_items = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priorité", 999)))
        display_df = pd.DataFrame(st.session_state.cargo_items)
        
    # --- TABLEAU ÉDITABLE ---
    edited_df = st.data_editor(
        display_df,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "Priorité": st.column_config.NumberColumn(min_value=1, step=1),
            "Quantité": st.column_config.NumberColumn(min_value=1, step=1),
            "Rotation": st.column_config.SelectboxColumn(options=["Aucune", "Horizontale", "Toutes"]),
            "Empilable": st.column_config.SelectboxColumn(options=["Oui", "Non"])
        }
    )
    # On met à jour l'état avec les modifications éventuelles faites à la main dans le tableau
    st.session_state.cargo_items = edited_df.to_dict('records')

    st.header("4. Résultat de l'optimisation")
    if st.button("🚀 Calculer et afficher la flotte", type="primary", use_container_width=True):
        if not st.session_state.cargo_items:
            st.warning("Veuillez ajouter des marchandises.")
        else:
            # --- VERIFICATION PRE-CALCUL (SANITY CHECK MILLIMETRES) ---
            impossible_items = set()
            for item in st.session_state.cargo_items:
                h = float(item["Hauteur"])
                rot = item.get("Rotation", "Horizontale")
                if rot in ["Aucune", "Horizontale"] and h > c_props["h"]:
                    impossible_items.add(item["Référence"])
            
            if impossible_items:
                st.error(f"🚨 **ALERTE DIMENSION** : La hauteur des articles suivants ({', '.join(impossible_items)}) dépasse le plafond du camion ({c_props['h']} cm). \n\n👉 **Avez-vous saisi des millimètres (ex: 1060) au lieu de centimètres (ex: 106) ?** L'algorithme refusera de les charger.")
            
            with st.spinner("Calcul en cours (Règles métiers + Heuristique)..."):
                
                # Le tableau a pu être édité, on s'assure qu'il est bien trié
                sorted_cargo = sorted(st.session_state.cargo_items, key=lambda x: int(x.get("Priorité", 999)))
                
                # 1. On prépare la liste absolue de TOUTES les boîtes en PRÉ-EMPILANT les palettes
                all_items_to_pack = []
                for item in sorted_cargo:
                    rot_val = item.get("Rotation", "Horizontale")
                    if rot_val == "Aucune": allowed_rot = [0]
                    elif rot_val == "Horizontale": allowed_rot = [0, 1]
                    else: allowed_rot = [0, 1, 2, 3, 4, 5]
                    
                    is_stackable = True if item.get("Empilable", "Oui") == "Oui" else False
                    
                    # Forcer le type Decimal pour éviter l'erreur TypeError interne
                    h = Decimal(str(item["Hauteur"]))
                    weight = Decimal(str(item["Poids"]))
                    qty = int(item["Quantité"])
                    l_dec = Decimal(str(item["Longueur"]))
                    w_dec = Decimal(str(item["Largeur"]))
                    
                    ref_name = item["Référence"]
                    if ref_name not in st.session_state.color_map:
                        st.session_state.color_map[ref_name] = DISTINCT_COLORS[len(st.session_state.color_map) % len(DISTINCT_COLORS)]

                    # PRÉ-EMPILAGE (Virtual Stacking)
                    if is_stackable and rot_val in ["Aucune", "Horizontale"]:
                        max_stack = int(Decimal(str(c_props["h"])) // h)
                        max_stack = max(1, min(max_stack, int(Decimal(str(c_props["poids_max"])) // weight) if weight > 0 else 999))
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
                        c_item.original_hauteur = h
                        c_item.original_weight = weight
                        
                        all_items_to_pack.append(c_item)
                        qty -= current_q

                unpacked_items = list(all_items_to_pack)
                used_bins = []

                # 2. Remplissage manuel camion par camion
                for i in range(int(max_bins)):
                    if not unpacked_items:
                        break # Tout est chargé !
                    
                    # Forcer le type Decimal pour les dimensions et poids max du conteneur
                    bin_obj = Bin(
                        f"{transport_type} #{i+1}", 
                        Decimal(str(c_props["L"])), 
                        Decimal(str(c_props["l"])), 
                        Decimal(str(c_props["h"])), 
                        Decimal(str(c_props["poids_max"]))
                    )
                    
                    # Génération des slots parfaits pour Europalette dans CE camion
                    euro_slots = get_optimal_europallet_slots(transport_type)
                    
                    items_left = []
                    # Insertion respectant l'ordre de priorité, les Règles Europalette, ET l'optimisation
                    for item in unpacked_items:
                        success = pack_with_rules(bin_obj, item, euro_slots)
                        if not success:
                            items_left.append(item)
                    
                    if len(bin_obj.items) > 0:
                        used_bins.append(bin_obj)
                    
                    # On transfère ce qui n'est pas rentré dans le prochain camion
                    unpacked_items = items_left

                # 3. DÉBALLAGE DES COLONNES VIRTUELLES POUR LE VISUEL (Unpacking)
                final_used_bins = []
                for b in used_bins:
                    unpacked_items_list = []
                    for stack_item in b.items:
                        orig_q = getattr(stack_item, 'original_qty', 1)
                        base_name = stack_item.name.split(" #Stack")[0]
                        
                        if orig_q > 1:
                            orig_h = getattr(stack_item, 'original_hauteur')
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

                # Déballage des items refusés
                final_unfitted = []
                for stack_item in unpacked_items:
                    orig_q = getattr(stack_item, 'original_qty', 1)
                    base_name = stack_item.name.split(" #Stack")[0]
                    if orig_q > 1:
                        orig_h = getattr(stack_item, 'original_hauteur')
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

                # --- AFFICHAGE DES RÉSULTATS ---
                st.success(f"✅ Optimisation terminée ! {len(used_bins)} véhicule(s) utilisé(s).")
                
                if len(unpacked_items) > 0:
                    st.error(f"❌ La flotte de {max_bins} véhicule(s) est pleine (ou certaines palettes sont trop grandes) ! {len(unpacked_items)} article(s) restent à quai.")
                
                for b in used_bins:
                    total_vol = float(b.width * b.height * b.depth)
                    used_vol = sum([float(i.width * i.height * i.depth) for i in b.items])
                    fill_rate = (used_vol / total_vol) * 100 if total_vol > 0 else 0
                    
                    st.markdown(f"### 🚛 {b.name} (Rempli à {fill_rate:.1f}%)")
                    st.caption(f"{len(b.items)} articles chargés dans ce véhicule.")
                    
                    fig = plot_3d_packing(
                        (c_props["L"], c_props["l"], c_props["h"]), 
                        b.items, 
                        st.session_state.color_map,
                        title=f"Vue 3D - {b.name}"
                    )
                    st.plotly_chart(fig, use_container_width=True)
