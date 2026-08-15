"""
ETL: Stardew Valley save (XML) -> data.json para o dashboard operacional.
Uso: python parse_save.py
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

SAVE_DIR = Path(__file__).resolve().parent.parent
SAVE_FILE = SAVE_DIR / "Duarte_443237868"
OUT_FILE = Path(__file__).resolve().parent / "data.json"

SEASONS = {"spring": "Primavera", "summer": "Verão", "fall": "Outono", "winter": "Inverno"}
SEASON_BY_INDEX = ["Primavera", "Verão", "Outono", "Inverno"]

import re
PHANTOM_LOCATION_RE = re.compile(r"^Cellar\d*$")  # slots de adega reservados p/ até 8 jogadores
# em multiplayer; o jogo pré-popula cada um com o mesmo layout padrão (33 casks) mesmo
# sem cabana/adega construída. Se "Cellar" não aparece em player/locationsVisited, é
# dado fantasma do formato de save, não algo que o jogador realmente tem.


def is_real_location(name):
    return not PHANTOM_LOCATION_RE.match(name or "")


def season_name(raw):
    if raw is None:
        return None
    if raw in SEASONS:
        return SEASONS[raw]
    try:
        return SEASON_BY_INDEX[int(raw) % 4]
    except (ValueError, IndexError):
        return raw

# Não fixamos aqui um "total de conquistas do jogo": não dá pra confirmar esse número
# só a partir do save, e o jogo já teve versões com contagens diferentes. Mostramos
# apenas os ids desbloqueados (dado 100% real) e a contagem absoluta, sem meta/%.


def txt(el, tag, default=None):
    if el is None:
        return default
    c = el.find(tag)
    return c.text if c is not None and c.text is not None else default


def build_item_name_map(root):
    mapping = {}
    for el in root.iter():
        iid = el.find("itemId")
        nm = el.find("name")
        if iid is not None and nm is not None and iid.text and nm.text:
            mapping.setdefault(iid.text, nm.text)
    return mapping



# Ids de objeto GENÉRICOS que o próprio jogo reusa para vários produtos diferentes:
# todo vinho de fruta = 348, toda geleia de fruta = 344, toda conserva de vegetal = 342,
# todo suco de vegetal = 350. O nome específico ("Melon Wine") é só um display por
# instância — o contador cumulativo `basicShipped` do save NÃO distingue qual fruta,
# só guarda a soma sob o id genérico. Não é possível recuperar o mix exato só com esse
# contador; usamos o preço MÉDIO das instâncias observadas no save atual como proxy
# (mais realista que o preço máximo, que supercontaria o item mais caro do grupo).
GENERIC_ARTISAN_IDS = {
    "348": "Vinho (fruta variada)",
    "344": "Geleia (fruta variada)",
    "342": "Conserva (vegetal variado)",
    "350": "Suco (vegetal variado)",
}


def build_item_price_map(root):
    """itemId -> preço base de venda. Para ids genéricos (ver GENERIC_ARTISAN_IDS)
    usa a MÉDIA dos preços observados; para os demais, usa o preço mais frequente
    (na prática só há um preço por id não-genérico)."""
    samples = {}
    for el in root.iter():
        iid = el.find("itemId")
        price = el.find("price")
        if iid is not None and price is not None and iid.text and price.text:
            try:
                p = int(price.text)
            except ValueError:
                continue
            if p > 0:
                samples.setdefault(iid.text, []).append(p)
    mapping = {}
    for iid, prices in samples.items():
        if iid in GENERIC_ARTISAN_IDS:
            mapping[iid] = round(sum(prices) / len(prices))
        else:
            mapping[iid] = max(prices)
    return mapping


QUALITY_MULT = {0: 1.0, 1: 1.25, 2: 1.5, 4: 2.0}
ARTISAN_MACHINES = (
    "Cask", "Keg", "Preserves Jar", "Tapper", "Cheese Press", "Mayonnaise Machine",
    "Loom", "Oil Maker", "Furnace", "Recycling Machine", "Bee House", "Charcoal Kiln",
    "Crystalarium", "Seed Maker", "Slime Egg-Press", "Ostrich Incubator", "Incubator",
    "Bone Mill", "Crab Pot",
)
MINUTES_PER_DAY = 1200  # dia em SDV = 6h-2h = 1200 minutos


def item_sell_value(item_el, price_map, name_map):
    """Valor de venda (price * quality_mult * stack) para um <Item>/<Object>."""
    item_id = txt(item_el, "itemId")
    name = txt(item_el, "name") or name_map.get(item_id, f"Item #{item_id}")
    price_el = item_el.find("price")
    price = int(price_el.text) if price_el is not None and price_el.text else price_map.get(item_id, 0)
    quality = int(txt(item_el, "quality", 0) or 0)
    stack = int(txt(item_el, "stack", 1) or 1)
    mult = QUALITY_MULT.get(quality, 1.0)
    value = price * mult * stack
    return name, value, stack


FRUIT_CATEGORY = "-79"
VEGETABLE_CATEGORY = "-75"
ARTISAN_GOODS_CATEGORY = "-26"
SYRUP_CATEGORY = "-27"  # Maple Syrup / Oak Resin / Pine Tar (produção de Tapper)
PROCESSABLE_MACHINES = ("Keg", "Preserves Jar")  # transformam matéria-prima crua
AGING_MACHINES = ("Cask",)  # envelhecem bebida já feita (não consomem matéria-prima crua)


def best_processed_value(price, category):
    """Melhor opção de processamento (Keg ou Preserves Jar) para 1 unidade de matéria-prima crua."""
    options = []
    if category == FRUIT_CATEGORY:
        options.append(("Keg -> Vinho", price * 3))
        options.append(("Jar -> Geleia", price * 2 + 50))
    elif category == VEGETABLE_CATEGORY:
        options.append(("Keg -> Suco", price * 2.25))
        options.append(("Jar -> Conserva", price * 2 + 50))
    if not options:
        return None, 0
    return max(options, key=lambda x: x[1])


def scan_stock_value(root, price_map, name_map):
    """Percorre TODAS as localizações separando:
    - matéria-prima crua em estoque (baús + mochila), com potencial de processamento em Keg/Jar
    - produtos artesanais já em estoque (prontos para vender)
    - produção em andamento dentro das máquinas (fermentando/curando)
    """
    raw_by_item = {}       # nome -> {stack, price, category, value, potential}
    artisan_stock_by_item = {}
    raw_total = 0.0
    raw_potential_total = 0.0
    artisan_stock_total = 0.0
    brewing_total = 0.0
    ready_total = 0.0
    machine_value_counts = {"ready": 0, "brewing": 0}

    def add_raw(name, category, price, stack, quality_mult):
        nonlocal raw_total, raw_potential_total
        value = price * quality_mult * stack
        if value <= 0:
            return
        raw_total += value
        label, unit_value = best_processed_value(price * quality_mult, category)
        potential = unit_value * stack
        raw_potential_total += potential
        e = raw_by_item.setdefault(name, {"name": name, "stack": 0, "value": 0.0, "potential": 0.0, "method": label})
        e["stack"] += stack
        e["value"] += value
        e["potential"] += potential

    def add_artisan(name, value):
        nonlocal artisan_stock_total
        if value <= 0:
            return
        artisan_stock_total += value
        artisan_stock_by_item[name] = artisan_stock_by_item.get(name, 0) + value

    def classify_and_add(it):
        item_id = txt(it, "itemId")
        category = txt(it, "category")
        price_el = it.find("price")
        price = int(price_el.text) if price_el is not None and price_el.text else price_map.get(item_id, 0)
        quality = int(txt(it, "quality", 0) or 0)
        stack = int(txt(it, "stack", 1) or 1)
        mult = QUALITY_MULT.get(quality, 1.0)
        name = txt(it, "name") or name_map.get(item_id, f"Item #{item_id}")
        if category in (FRUIT_CATEGORY, VEGETABLE_CATEGORY):
            add_raw(name, category, price, stack, mult)
        elif category in (ARTISAN_GOODS_CATEGORY, SYRUP_CATEGORY):
            add_artisan(name, price * mult * stack)
        # outras categorias (gemas, minérios, loot de monstro, ferramentas) ficam fora
        # desta análise financeira de matéria-prima/artesanato

    def walk_object(obj):
        nonlocal brewing_total, ready_total
        name = txt(obj, "name")
        if name == "Chest":
            items_el = obj.find("items")
            if items_el is not None:
                for it in list(items_el):
                    classify_and_add(it)
            return
        if name in ARTISAN_MACHINES:
            held = obj.find("heldObject")
            if held is None:
                return
            inner = held if held.find("name") is not None else held.find(".//Object")
            if inner is None:
                return
            nm, val, _ = item_sell_value(inner, price_map, name_map)
            minutes_left = int(txt(obj, "minutesUntilReady", 0) or 0)
            if minutes_left <= 0:
                ready_total += val
                machine_value_counts["ready"] += 1
            else:
                brewing_total += val
                machine_value_counts["brewing"] += 1

    for gl in root.find("locations").findall("GameLocation"):
        if not is_real_location(txt(gl, "name")):
            continue
        objects = gl.find("objects")
        if objects is not None:
            for item in objects.findall("item"):
                obj = item.find(".//Object")
                if obj is not None:
                    walk_object(obj)
        buildings = gl.find("buildings")
        if buildings is not None:
            for b in buildings.findall("Building"):
                indoors = b.find("indoors")
                if indoors is None:
                    continue
                objects2 = indoors.find("objects")
                if objects2 is not None:
                    for item in objects2.findall("item"):
                        obj = item.find(".//Object")
                        if obj is not None:
                            walk_object(obj)

    # inventário do jogador (mochila)
    player = root.find("player")
    items_el = player.find("items")
    if items_el is not None:
        for it in list(items_el):
            xsi_type = it.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
            if xsi_type not in ("Object", "ColoredObject"):
                continue  # ignora ferramentas, roupas, armas
            classify_and_add(it)

    top_raw = sorted(raw_by_item.values(), key=lambda x: -x["value"])[:10]
    top_artisan = sorted(artisan_stock_by_item.items(), key=lambda x: -x[1])[:10]

    return {
        "rawValue": round(raw_total, 0),
        "rawPotentialValue": round(raw_potential_total, 0),
        "artisanStockValue": round(artisan_stock_total, 0),
        "readyValue": round(ready_total, 0),
        "brewingValue": round(brewing_total, 0),
        "machinesReady": machine_value_counts["ready"],
        "machinesBrewing": machine_value_counts["brewing"],
        "topRawItems": [{"name": r["name"], "stack": r["stack"], "value": round(r["value"], 0),
                          "potential": round(r["potential"], 0), "method": r["method"]} for r in top_raw],
        "topArtisanItems": [{"name": n, "value": round(v, 0)} for n, v in top_artisan],
        "rawUnitsTotal": sum(r["stack"] for r in raw_by_item.values()),
    }


def parse_overview(player):
    season = txt(player, "seasonForSaveGame", "0")
    return {
        "playerName": txt(player, "name"),
        "farmName": txt(player, "farmName"),
        "money": int(txt(player, "money", 0)),
        "totalMoneyEarned": int(txt(player, "totalMoneyEarned", 0)),
        "qiGems": int(txt(player, "qiGems", 0)),
        "day": int(txt(player, "dayOfMonthForSaveGame", 1)),
        "season": season_name(season),
        "year": int(txt(player, "yearForSaveGame", 1)),
        "houseUpgradeLevel": int(txt(player, "houseUpgradeLevel", 0)),
    }


def parse_skills(player):
    return {
        "farming": int(txt(player, "farmingLevel", 0)),
        "mining": int(txt(player, "miningLevel", 0)),
        "combat": int(txt(player, "combatLevel", 0)),
        "foraging": int(txt(player, "foragingLevel", 0)),
        "fishing": int(txt(player, "fishingLevel", 0)),
        "luck": int(txt(player, "luckLevel", 0)),
    }


def parse_stats(player):
    stats_el = player.find("stats")
    values = {}
    monsters = {}
    if stats_el is not None:
        vals = stats_el.find("Values")
        if vals is not None:
            for item in vals.findall("item"):
                key = item.find("key/string")
                val = item.find("value")
                if key is not None and val is not None:
                    v = list(val)[0] if len(list(val)) else val
                    try:
                        values[key.text] = int(v.text)
                    except (TypeError, ValueError):
                        values[key.text] = v.text
        sm = stats_el.find("specificMonstersKilled")
        if sm is not None:
            for item in sm.findall("item"):
                key = item.find("key/string")
                val = item.find("value/int")
                if key is not None and val is not None:
                    monsters[key.text] = int(val.text)
    return values, monsters


def parse_achievements(player):
    """Só os ids desbloqueados (dado real e exato do save). Sem nomes/descrições:
    não há como confirmar o texto oficial de cada id sem uma fonte externa."""
    ach_el = player.find("achievements")
    ids = []
    if ach_el is not None:
        ids = [int(e.text) for e in ach_el.findall("int")]
    return sorted(ids)


# Localizações que o próprio jogo pré-instancia mesmo sem o jogador ter acesso —
# não contam como "lugar real para explorar" (ver PHANTOM_LOCATION_RE acima).
# Para as demais, usamos flags reais do save (posse de chave/item, correio recebido)
# para dizer se o motivo de não ter visitado é "ainda trancado" ou genuinamente
# "dá pra ir lá e ainda não foi".
BOAT_GATED = {
    "BoatTunnel", "CaptainRoom", "IslandEast", "IslandFarmCave", "IslandFarmHouse",
    "IslandFieldOffice", "IslandHut", "IslandNorth", "IslandNorthCave1", "IslandShrine",
    "IslandSouth", "IslandSouthEast", "IslandSouthEastCave", "IslandWest", "IslandWestCave1",
    "LeoTreeHouse", "QiNutRoom", "MasteryCave", "Caldera",
}


def parse_exploration(root):
    player = root.find("player")
    visited = set(s.text for s in player.find("locationsVisited").findall("string"))
    farm = get_farm_location(root)
    greenhouse_unlocked = txt(farm, "greenhouseUnlocked") == "true" if farm is not None else False
    has_rusty_key = txt(player, "hasRustyKey") == "true"
    has_club_card = txt(player, "hasClubCard") == "true"
    has_skull_key = txt(player, "hasSkullKey") == "true"
    has_dark_talisman = txt(player, "hasDarkTalisman") == "true"
    has_magic_ink = txt(player, "hasMagicInk") == "true"
    mail = set(s.text for s in player.find("mailReceived").findall("string")) if player.find("mailReceived") is not None else set()
    boat_fixed = "Boat_Fixed" in mail

    existing = [txt(gl, "name") for gl in root.find("locations").findall("GameLocation")]
    existing = [n for n in existing if is_real_location(n)]

    result = []
    for name in sorted(set(existing) - visited):
        reason = None
        if name in BOAT_GATED and not boat_fixed:
            reason = "Requer conserto do barco (Ilha Ginger)"
        elif name == "Greenhouse" and not greenhouse_unlocked:
            reason = "Estufa ainda não desbloqueada"
        elif name in ("Sewer", "BugLand") and not has_rusty_key:
            reason = "Requer a Chave Enferrujada"
        elif name == "Club" and not has_club_card:
            reason = "Requer o Cartão do Clube"
        elif name == "SkullCave" and not has_skull_key:
            reason = "Requer a Chave da Caveira"
        elif name in ("WitchHut", "WitchSwamp", "WitchWarpCave") and not has_dark_talisman:
            reason = "Requer o Talismã Sombrio"
        elif name == "WizardHouseBasement" and not has_magic_ink:
            reason = "Requer a Tinta Mágica"
        result.append({"name": name, "locked": reason is not None, "reason": reason})

    result.sort(key=lambda x: (not x["locked"], x["name"]))
    return {
        "totalExisting": len(set(existing)),
        "totalVisited": len(visited & set(existing)),
        "notVisited": result,
    }


def parse_friendships(player):
    fd = player.find("friendshipData")
    result = []
    if fd is not None:
        for item in fd.findall("item"):
            name = txt(item.find("key"), "string")
            fr = item.find("value/Friendship")
            if fr is None:
                continue
            points = int(txt(fr, "Points", 0))
            status = txt(fr, "Status", "Friendly")
            last_gift = fr.find("LastGiftDate")
            last_gift_str = None
            if last_gift is not None:
                d = txt(last_gift, "DayOfMonth")
                s = txt(last_gift, "Season")
                y = txt(last_gift, "Year")
                if d and s and y:
                    last_gift_str = f"{SEASONS.get(s, s)} {d}, Ano {y}"
            result.append({
                "name": name,
                "points": points,
                "hearts": points // 250,
                "status": status,
                "lastGift": last_gift_str,
            })
    result.sort(key=lambda x: -x["points"])
    return result


def parse_basic_shipped(player, name_map):
    """Quantidade enviada por item — o contador em si (`basicShipped`) é real e exato.
    Não calculamos valor em dinheiro aqui: para vinho/geleia/conserva/suco o jogo agrupa
    várias frutas/vegetais diferentes sob o mesmo id (ver GENERIC_ARTISAN_IDS), então não
    há preço confiável a atribuir por item — só mostramos a quantidade real."""
    bs = player.find("basicShipped")
    result = []
    if bs is not None:
        for item in bs.findall("item"):
            item_id = txt(item.find("key"), "string")
            qty = int(txt(item.find("value"), "int", 0))
            name = GENERIC_ARTISAN_IDS.get(item_id) or name_map.get(item_id, f"Item #{item_id}")
            result.append({"id": item_id, "name": name, "qty": qty, "isGenericId": item_id in GENERIC_ARTISAN_IDS})
    result.sort(key=lambda x: -x["qty"])
    return result


def get_farm_location(root):
    locs = root.find("locations")
    for gl in locs.findall("GameLocation"):
        if txt(gl, "name") == "Farm":
            return gl
    return None


def get_location(root, name):
    locs = root.find("locations")
    for gl in locs.findall("GameLocation"):
        if txt(gl, "name") == name:
            return gl
    return None


SEASON_LENGTH_DAYS = 28  # fato fixo do jogo: toda estação tem 28 dias
REGROW_SENTINEL = 90000  # fase final >= isso = "aguardando regrow", plantação já perene


def parse_crops(farm, greenhouse, name_map, price_map, current_day):
    """Retorna estatísticas de plantações + risco de morte por virada de estação
    (fato de jogo: plantação não colhida na virada da estação morre) + ROI por tile
    (g de venda / dias até a 1a colheita, direto dos campos phaseDays do save).
    A Estufa é uma localização separada da Farm no save — plantações lá dentro não
    aparecem em farm.find('terrainFeatures'), por isso é escaneada junto aqui.
    Culturas na Estufa nunca morrem por virada de estação (regra do jogo: cultivo
    protegido), então ficam de fora do cálculo de risco mesmo se ainda imaturas."""
    locations_to_scan = [(farm, False)]
    if greenhouse is not None:
        locations_to_scan.append((greenhouse, True))
    crops = {}
    total_tiles = 0
    fully_grown = 0
    dead = 0
    days_left_in_season = SEASON_LENGTH_DAYS - current_day
    at_risk = {}  # nome -> {count, value}
    roi_data = {}  # nome -> {totalDays, price, count}

    for loc, is_greenhouse in locations_to_scan:
        tf = loc.find("terrainFeatures")
        if tf is None:
            continue
        for item in tf.findall("item"):
            hoedirt = item.find(".//TerrainFeature[@{http://www.w3.org/2001/XMLSchema-instance}type='HoeDirt']")
            if hoedirt is None:
                continue
            crop = hoedirt.find("crop")
            if crop is None:
                continue
            total_tiles += 1
            seed_id = txt(crop, "seedIndex") or txt(crop, "indexOfHarvest")
            harvest_id = txt(crop, "indexOfHarvest")
            crop_name = name_map.get(harvest_id, name_map.get(seed_id, f"Plantação #{harvest_id or seed_id}"))
            is_dead = txt(crop, "dead") == "true"
            phase_days_el = crop.find("phaseDays")
            phase_values = [int(p.text) for p in phase_days_el.findall("int")] if phase_days_el is not None else []
            n_phases = len(phase_values)
            current_phase = int(txt(crop, "currentPhase", 0))
            day_of_phase = int(txt(crop, "dayOfCurrentPhase", 0))
            # "dead" tem prioridade: o jogo mantém a flag fullGrown=true de um ciclo
            # anterior mesmo depois da planta morrer (comum em culturas que rebrotam,
            # ex: Hot Pepper) — sem isso, a mesma tile contava como madura E morta.
            full = not is_dead and ((txt(crop, "fullGrown") == "true") or current_phase >= max(n_phases - 1, 0))
            if full:
                fully_grown += 1
            if is_dead:
                dead += 1
            crops.setdefault(crop_name, {"name": crop_name, "count": 0, "fullyGrown": 0, "dead": 0})
            crops[crop_name]["count"] += 1
            if full:
                crops[crop_name]["fullyGrown"] += 1
            if is_dead:
                crops[crop_name]["dead"] += 1

            # ROI: dias até a 1a colheita (soma das fases, ignorando o sentinela de regrow) + preço de venda
            first_cycle_phases = [p for p in phase_values if p < REGROW_SENTINEL]
            total_growth_days = sum(first_cycle_phases)
            harvest_price = price_map.get(harvest_id, 0)
            if total_growth_days > 0 and harvest_price > 0:
                r = roi_data.setdefault(crop_name, {"totalDays": total_growth_days, "price": harvest_price, "count": 0})
                r["count"] += 1

            # Risco de fim de estação: plantação ainda não madura, sem dias suficientes p/ terminar.
            # Não aplica à Estufa — cultivo lá dentro não morre na virada de estação.
            if not full and not is_dead and not is_greenhouse:
                remaining_phases = phase_values[current_phase:]
                remaining_days = sum(p for p in remaining_phases if p < REGROW_SENTINEL) - day_of_phase
                if remaining_days > days_left_in_season:
                    e = at_risk.setdefault(crop_name, {"name": crop_name, "count": 0, "unitValue": price_map.get(harvest_id, 0)})
                    e["count"] += 1

    crop_list = sorted(crops.values(), key=lambda x: -x["count"])

    roi_list = []
    for name, r in roi_data.items():
        roi_list.append({
            "name": name, "daysToHarvest": r["totalDays"], "price": r["price"],
            "roiPerDay": round(r["price"] / r["totalDays"], 1), "tiles": r["count"],
        })
    roi_list.sort(key=lambda x: -x["roiPerDay"])

    at_risk_list = sorted(at_risk.values(), key=lambda x: -x["count"])
    at_risk_total_tiles = sum(e["count"] for e in at_risk_list)
    at_risk_total_value = sum(e["count"] * e["unitValue"] for e in at_risk_list)

    return {
        "byType": crop_list,
        "totalTiles": total_tiles,
        "fullyGrown": fully_grown,
        "dead": dead,
        "growing": total_tiles - fully_grown - dead,
        "daysLeftInSeason": days_left_in_season,
        "seasonEndRisk": {
            "tilesAtRisk": at_risk_total_tiles,
            "valueAtRisk": round(at_risk_total_value, 0),
            "byType": at_risk_list,
        },
        "roiPerTile": roi_list,
    }


SPRINKLER_COVERAGE = {"Sprinkler": 4, "Quality Sprinkler": 8, "Iridium Sprinkler": 24}


def parse_sprinklers(farm, greenhouse):
    """Conta sprinklers e soma a capacidade de cobertura (fato: 4/8/24 tiles por tipo,
    valores fixos do jogo). É um teto de capacidade, não a cobertura real tile-a-tile
    (sprinklers podem se sobrepor). Inclui a Estufa — localização separada da Farm
    no save, mas onde é comum ter sprinkler instalado."""
    counts = {t: 0 for t in SPRINKLER_COVERAGE}
    for loc in (farm, greenhouse):
        if loc is None:
            continue
        objects = loc.find("objects")
        if objects is None:
            continue
        for item in objects.findall("item"):
            obj = item.find(".//Object")
            if obj is None:
                continue
            name = txt(obj, "name")
            if name in SPRINKLER_COVERAGE:
                counts[name] += 1
    capacity = sum(counts[t] * SPRINKLER_COVERAGE[t] for t in counts)
    return {"counts": counts, "coverageCapacity": capacity}


def parse_animals(farm):
    buildings = farm.find("buildings")
    animals = []
    building_counts = {}
    if buildings is not None:
        for b in buildings.findall("Building"):
            btype = txt(b, "buildingType", "?")
            building_counts[btype] = building_counts.get(btype, 0) + 1
            indoors = b.find("indoors")
            if indoors is None:
                continue
            an_el = indoors.find("animals")
            if an_el is None:
                continue
            for item in an_el.findall("item"):
                a = item.find(".//FarmAnimal")
                if a is None:
                    continue
                friendship = int(txt(a, "friendshipTowardFarmer", 0))
                happiness = int(txt(a, "happiness", 0))
                animals.append({
                    "name": txt(a, "name", "?").strip(),
                    "type": txt(a, "type", "?"),
                    "building": btype,
                    "friendship": friendship,
                    "friendshipPct": round(friendship / 1000 * 100, 1),
                    "happiness": happiness,
                    "happinessPct": round(happiness / 255 * 100, 1),
                })
    return animals, building_counts


def parse_production_objects(root):
    """Conta objetos artesanais (Kegs, Casks, Preserves Jars, Tappers, etc.) em
    TODAS as localizações do save — não só na Farm. Máquinas funcionam mesmo
    guardadas dentro de casa/estufa/celeiro, então contar só o mapa da fazenda
    subestima o total real (ex: Preserves Jars deixadas dentro da FarmHouse)."""
    counts = {}
    active = {}

    def scan_objects(objects_el):
        if objects_el is None:
            return
        for item in objects_el.findall("item"):
            obj = item.find(".//Object")
            if obj is None:
                continue
            name = txt(obj, "name")
            if name is None:
                continue
            if name in ("Cask", "Keg", "Preserves Jar", "Tapper", "Cheese Press", "Mayonnaise Machine",
                        "Loom", "Oil Maker", "Furnace", "Recycling Machine", "Bee House", "Charcoal Kiln",
                        "Crystalarium", "Seed Maker", "Slime Egg-Press", "Ostrich Incubator", "Incubator",
                        "Bone Mill", "Crab Pot"):
                counts[name] = counts.get(name, 0) + 1
                if obj.find("heldObject") is not None:
                    active[name] = active.get(name, 0) + 1

    for gl in root.find("locations").findall("GameLocation"):
        if not is_real_location(txt(gl, "name")):
            continue
        scan_objects(gl.find("objects"))
        buildings = gl.find("buildings")
        if buildings is not None:
            for b in buildings.findall("Building"):
                indoors = b.find("indoors")
                if indoors is not None:
                    scan_objects(indoors.find("objects"))

    return counts, active


def next_round_target(value, steps=(15000, 50000, 100000, 250000, 500000, 1000000, 2000000, 5000000)):
    for s in steps:
        if value < s:
            return s
    # acima do maior step: proximo milhao redondo
    return (int(value // 1000000) + 1) * 1000000


def build_kpis(overview, skills, stats, crops, machines_counts, machines_active, friendships, finance, sprinklers):
    days_played = stats.get("daysPlayed", 1) or 1
    daily_run_rate = overview["totalMoneyEarned"] / days_played

    idle_machines = sum(v - machines_active.get(k, 0) for k, v in machines_counts.items())
    total_machines = sum(machines_counts.values()) or 1
    machine_uptime_pct = round((total_machines - idle_machines) / total_machines * 100, 1)

    mature_pct = round(crops.get("fullyGrown", 0) / max(crops.get("totalTiles", 1), 1) * 100, 1)
    dead_pct = round(crops.get("dead", 0) / max(crops.get("totalTiles", 1), 1) * 100, 1)

    avg_hearts = round(sum(f["hearts"] for f in friendships) / max(len(friendships), 1), 1)
    npcs_maxed = sum(1 for f in friendships if f["hearts"] >= 10)

    skill_vals = list(skills.values())
    skill_avg = round(sum(skill_vals) / len(skill_vals), 1)
    skill_min_name = min(skills.items(), key=lambda kv: kv[1])

    irrigation_pct = round(min(sprinklers.get("coverageCapacity", 0) / max(crops.get("totalTiles", 1), 1) * 100, 100), 1)
    season_risk_pct = round(crops.get("seasonEndRisk", {}).get("tilesAtRisk", 0) / max(crops.get("totalTiles", 1), 1) * 100, 1)

    return {
        "money": {"label": "Dinheiro em caixa", "target": next_round_target(overview["money"]), "actual": overview["money"], "unit": "g"},
        "totalEarned": {"label": "Total ganho (histórico)", "target": next_round_target(overview["totalMoneyEarned"]), "actual": overview["totalMoneyEarned"], "unit": "g"},
        "skillAvg": {"label": "Média das habilidades", "target": 10, "actual": skill_avg, "unit": "", "worst": skill_min_name[0]},
        "machineUptime": {"label": "Máquinas em produção", "target": 100, "actual": machine_uptime_pct, "unit": "%"},
        "cropMaturity": {"label": "Plantações maduras", "target": 100, "actual": mature_pct, "unit": "%"},
        "cropLoss": {"label": "Plantações mortas", "target": 0, "actual": dead_pct, "unit": "%", "inverse": True},
        "irrigation": {"label": "Cobertura de irrigação (teto)", "target": 100, "actual": irrigation_pct, "unit": "%"},
        "seasonRisk": {"label": "Tiles em risco na virada da estação", "target": 0, "actual": season_risk_pct, "unit": "%", "inverse": True},
        "friendshipAvg": {"label": "Média de corações (NPCs)", "target": 10, "actual": avg_hearts, "unit": "♥"},
        "npcsMaxed": {"label": "NPCs com 10♥+", "target": len(friendships), "actual": npcs_maxed, "unit": ""},
        "dailyRunRate": {"label": "Receita média/dia (histórico)", "target": None, "actual": round(daily_run_rate, 0), "unit": "g/dia"},
    }


def build_projection(overview, stats, finance, machines_counts, machines_active):
    days_played = stats.get("daysPlayed", 1) or 1
    daily_run_rate = overview["totalMoneyEarned"] / days_played  # média histórica real, não é previsão

    # capacidade de processamento: só Keg e Preserves Jar transformam matéria-prima crua
    keg_total = machines_counts.get("Keg", 0)
    keg_active = machines_active.get("Keg", 0)
    jar_total = machines_counts.get("Preserves Jar", 0)
    jar_active = machines_active.get("Preserves Jar", 0)
    cask_total = machines_counts.get("Cask", 0)
    cask_active = machines_active.get("Cask", 0)
    tapper_total = machines_counts.get("Tapper", 0)
    tapper_active = machines_active.get("Tapper", 0)

    # cenário A: vender tudo crua agora (matéria-prima) + produtos artesanais já prontos
    scenario_raw_now = finance["rawValue"] + finance["artisanStockValue"] + finance["readyValue"]
    # cenário B: processar toda matéria-prima em Keg/Jar antes de vender, + o que já tá pronto/fermentando
    scenario_processed = finance["rawPotentialValue"] + finance["artisanStockValue"] + finance["readyValue"] + finance["brewingValue"]
    processing_gain = scenario_processed - scenario_raw_now
    processing_gain_pct = round(processing_gain / scenario_raw_now * 100, 1) if scenario_raw_now > 0 else 0

    return {
        "dailyRunRate": round(daily_run_rate, 0),
        "machines": {
            "keg": {"total": keg_total, "active": keg_active, "idle": keg_total - keg_active},
            "jar": {"total": jar_total, "active": jar_active, "idle": jar_total - jar_active},
            "cask": {"total": cask_total, "active": cask_active, "idle": cask_total - cask_active},
            "tapper": {"total": tapper_total, "active": tapper_active, "idle": tapper_total - tapper_active},
        },
        "rawUnitsTotal": finance["rawUnitsTotal"],
        "scenarioSellRawNow": round(scenario_raw_now, 0),
        "scenarioProcessFirst": round(scenario_processed, 0),
        "processingGain": round(processing_gain, 0),
        "processingGainPct": processing_gain_pct,
        "brewingValue": finance["brewingValue"],
        "moneyIfSellRawNow": round(overview["money"] + scenario_raw_now, 0),
        "moneyIfProcessFirst": round(overview["money"] + scenario_processed, 0),
    }


def main():
    tree = ET.parse(SAVE_FILE)
    root = tree.getroot()
    player = root.find("player")
    name_map = build_item_name_map(root)
    price_map = build_item_price_map(root)

    farm = get_farm_location(root)
    greenhouse = get_location(root, "Greenhouse")
    overview = parse_overview(player)
    stats_values, monsters = parse_stats(player)
    crops = parse_crops(farm, greenhouse, name_map, price_map, overview["day"]) if farm is not None else {}
    sprinklers = parse_sprinklers(farm, greenhouse) if farm is not None else {}
    animals, building_counts = parse_animals(farm) if farm is not None else ([], {})
    machine_counts, machine_active = parse_production_objects(root)
    friendships = parse_friendships(player)
    achievements = parse_achievements(player)
    skills = parse_skills(player)

    finance = scan_stock_value(root, price_map, name_map)
    kpis = build_kpis(overview, skills, stats_values, crops, machine_counts, machine_active,
                       friendships, finance, sprinklers)
    projection = build_projection(overview, stats_values, finance, machine_counts, machine_active)
    shipped = parse_basic_shipped(player, name_map)
    exploration = parse_exploration(root)

    data = {
        "overview": overview,
        "skills": skills,
        "stats": stats_values,
        "monstersKilled": dict(sorted(monsters.items(), key=lambda x: -x[1])),
        "achievements": achievements,
        "friendships": friendships,
        "shipped": shipped,
        "crops": crops,
        "sprinklers": sprinklers,
        "animals": animals,
        "buildings": building_counts,
        "exploration": exploration,
        "machines": {"counts": machine_counts, "active": machine_active},
        "finance": finance,
        "kpis": kpis,
        "projection": projection,
    }

    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK -> {OUT_FILE} ({OUT_FILE.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
