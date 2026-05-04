"""Curated beverage catalog seed for the Phase 3 water tracker (DRF-301).

~60 entries covering water, tea, coffee, juice, soda, milk, alcohol,
broth, sport categories. Loaded by ``nutrition.management.commands.
seed_beverages`` via ``update_or_create(slug=...)`` so re-running is
idempotent and content edits made in admin survive a re-seed.

Sources:
- USDA FoodData Central (kcal, protein, fat, carbs, sugar, caffeine
  per 100 ml). Where USDA gives per-100g for liquids close to water
  density we use the same number.
- Beverage Hydration Index, Maughan et al. 2016, Am J Clin Nutr
  (water_coefficient). BHI(water)=1.00 by definition; values >1 mean
  *more* hydrating than water (milk 1.50). Negative coefficients are
  modelled for net-dehydrating spirits (vodka/whiskey) — POST /water/
  caps the resulting today_total at 0, not here.
- Скурихин-Тутельян «Химический состав российских пищевых продуктов»
  for RU staples (бульоны, ряженка, морс, квас).

Conventions:
- ``slug`` is lowercase ASCII with underscores. Stable across releases.
- ``aliases`` is a free-text list parsed by the bot's beverage matcher.
  Lower-case, ё→е, no punctuation; the model's save() lowercases.
- ``water_coefficient`` is a multiplier applied to ``ml``: hydration
  delivered = ml × water_coefficient.
- ``default_serving_ml`` / ``default_serving_label`` drive the
  acknowledgement string «+200 мл (чашка)» in the bot UI.

Numbers are catalog-grade approximations. Content owners adjust via
Django admin without redeploy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BeverageRow:
    slug: str
    name_ru: str
    category: str
    water_coefficient: float
    kcal_per_100ml: float = 0.0
    protein_g_per_100ml: float = 0.0
    fat_g_per_100ml: float = 0.0
    carbs_g_per_100ml: float = 0.0
    sugar_g_per_100ml: float = 0.0
    caffeine_mg_per_100ml: float = 0.0
    aliases: tuple[str, ...] = ()
    default_serving_ml: int = 250
    default_serving_label: str = "стакан"


BEVERAGES: tuple[BeverageRow, ...] = (
    # --- water ----------------------------------------------------------
    BeverageRow(
        "voda", "Вода", "water", 1.00,
        aliases=("вода", "water", "water still", "негазированная"),
        default_serving_ml=250, default_serving_label="стакан",
    ),
    BeverageRow(
        "voda_mineralnaya", "Минеральная вода", "water", 1.00,
        aliases=("минералка", "минеральная", "mineral water", "боржоми", "ессентуки"),
        default_serving_ml=500, default_serving_label="бутылка",
    ),
    BeverageRow(
        "voda_gazirovannaya", "Газированная вода", "water", 1.00,
        aliases=("газировка без сахара", "sparkling water", "газированная вода", "содовая"),
        default_serving_ml=330, default_serving_label="банка",
    ),

    # --- tea ------------------------------------------------------------
    BeverageRow(
        "chai_chernyi", "Чёрный чай", "tea", 1.00,
        kcal_per_100ml=1.0, caffeine_mg_per_100ml=20.0,
        aliases=("чай", "черный чай", "tea", "black tea"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "chai_zelenyi", "Зелёный чай", "tea", 1.00,
        kcal_per_100ml=1.0, caffeine_mg_per_100ml=12.0,
        aliases=("зеленый чай", "green tea", "матча", "matcha"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "chai_travyanoy", "Травяной чай", "tea", 1.00,
        kcal_per_100ml=0.5,
        aliases=("травяной", "herbal tea", "ромашка", "мята", "мелисса"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "chai_fruktovyi", "Фруктовый чай", "tea", 1.00,
        kcal_per_100ml=2.0,
        aliases=("фруктовый чай", "fruit tea", "ягодный чай"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "karkade", "Каркаде", "tea", 1.00,
        kcal_per_100ml=2.0,
        aliases=("каркаде", "hibiscus", "гибискус", "суданская роза"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "chai_s_molokom", "Чай с молоком", "tea", 1.10,
        kcal_per_100ml=27.0, protein_g_per_100ml=1.0,
        fat_g_per_100ml=1.0, carbs_g_per_100ml=3.5, sugar_g_per_100ml=3.5,
        caffeine_mg_per_100ml=15.0,
        aliases=("чай с молоком", "milk tea", "масала чай"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "mate", "Мате", "tea", 1.00,
        kcal_per_100ml=1.0, caffeine_mg_per_100ml=30.0,
        aliases=("мате", "yerba mate", "матэ"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "kombucha", "Комбуча", "tea", 0.95,
        kcal_per_100ml=14.0, carbs_g_per_100ml=3.5, sugar_g_per_100ml=3.0,
        caffeine_mg_per_100ml=8.0,
        aliases=("комбуча", "kombucha", "чайный гриб"),
        default_serving_ml=330, default_serving_label="бутылка",
    ),

    # --- coffee ---------------------------------------------------------
    BeverageRow(
        "kofe_chernyi", "Чёрный кофе", "coffee", 1.00,
        kcal_per_100ml=2.0, caffeine_mg_per_100ml=40.0,
        aliases=("кофе", "coffee", "черный кофе", "americano", "американо"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "espresso", "Эспрессо", "coffee", 0.90,
        kcal_per_100ml=9.0, protein_g_per_100ml=0.5,
        caffeine_mg_per_100ml=180.0,
        aliases=("эспрессо", "espresso"),
        default_serving_ml=30, default_serving_label="порция",
    ),
    BeverageRow(
        "latte", "Латте", "coffee", 1.10,
        kcal_per_100ml=42.0, protein_g_per_100ml=2.3,
        fat_g_per_100ml=1.5, carbs_g_per_100ml=4.7, sugar_g_per_100ml=4.7,
        caffeine_mg_per_100ml=30.0,
        aliases=("латте", "latte", "кофе латте"),
        default_serving_ml=240, default_serving_label="чашка",
    ),
    BeverageRow(
        "kapuchino", "Капучино", "coffee", 1.05,
        kcal_per_100ml=37.0, protein_g_per_100ml=2.0,
        fat_g_per_100ml=1.4, carbs_g_per_100ml=4.0, sugar_g_per_100ml=4.0,
        caffeine_mg_per_100ml=33.0,
        aliases=("капучино", "cappuccino"),
        default_serving_ml=180, default_serving_label="чашка",
    ),
    BeverageRow(
        "mokka", "Мокко", "coffee", 1.00,
        kcal_per_100ml=70.0, protein_g_per_100ml=2.0,
        fat_g_per_100ml=2.5, carbs_g_per_100ml=10.0, sugar_g_per_100ml=9.5,
        caffeine_mg_per_100ml=28.0,
        aliases=("мокко", "mocha", "мокачино"),
        default_serving_ml=240, default_serving_label="чашка",
    ),
    BeverageRow(
        "raf", "Раф", "coffee", 1.05,
        kcal_per_100ml=92.0, protein_g_per_100ml=2.5,
        fat_g_per_100ml=6.0, carbs_g_per_100ml=7.0, sugar_g_per_100ml=7.0,
        caffeine_mg_per_100ml=25.0,
        aliases=("раф", "раф кофе", "raf"),
        default_serving_ml=240, default_serving_label="чашка",
    ),
    BeverageRow(
        "kofe_rastvorimyi", "Растворимый кофе", "coffee", 1.00,
        kcal_per_100ml=2.0, caffeine_mg_per_100ml=30.0,
        aliases=("растворимый кофе", "instant coffee", "нескафе"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "kofe_bez_kofeina", "Кофе без кофеина", "coffee", 1.00,
        kcal_per_100ml=2.0, caffeine_mg_per_100ml=2.0,
        aliases=("декаф", "decaf", "без кофеина"),
        default_serving_ml=200, default_serving_label="чашка",
    ),

    # --- juice ----------------------------------------------------------
    BeverageRow(
        "sok_apelsinovyi", "Апельсиновый сок", "juice", 0.85,
        kcal_per_100ml=45.0, protein_g_per_100ml=0.7,
        carbs_g_per_100ml=10.4, sugar_g_per_100ml=8.4,
        aliases=("апельсиновый сок", "сок апельсин", "orange juice", "фреш апельсин"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sok_yablochnyi", "Яблочный сок", "juice", 0.85,
        kcal_per_100ml=46.0, carbs_g_per_100ml=11.3, sugar_g_per_100ml=9.6,
        aliases=("яблочный сок", "сок яблоко", "apple juice"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sok_tomatnyi", "Томатный сок", "juice", 0.95,
        kcal_per_100ml=17.0, protein_g_per_100ml=0.8,
        carbs_g_per_100ml=4.0, sugar_g_per_100ml=2.6,
        aliases=("томатный сок", "сок томат", "tomato juice"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sok_morkovnyi", "Морковный сок", "juice", 0.90,
        kcal_per_100ml=40.0, protein_g_per_100ml=0.8,
        carbs_g_per_100ml=9.3, sugar_g_per_100ml=3.9,
        aliases=("морковный сок", "carrot juice", "сок морковь"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sok_vishnyovyi", "Вишнёвый сок", "juice", 0.85,
        kcal_per_100ml=49.0, carbs_g_per_100ml=12.0, sugar_g_per_100ml=10.0,
        aliases=("вишневый сок", "вишня сок", "cherry juice"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sok_multifruktovyi", "Мультифруктовый сок", "juice", 0.85,
        kcal_per_100ml=47.0, carbs_g_per_100ml=11.5, sugar_g_per_100ml=10.0,
        aliases=("мультифрукт", "сок мультифрукт", "multifruit"),
        default_serving_ml=200, default_serving_label="стакан",
    ),

    # --- soda -----------------------------------------------------------
    BeverageRow(
        "cola", "Кола", "soda", 0.85,
        kcal_per_100ml=42.0, carbs_g_per_100ml=10.6, sugar_g_per_100ml=10.6,
        caffeine_mg_per_100ml=10.0,
        aliases=("кола", "coca-cola", "coke", "пепси", "pepsi"),
        default_serving_ml=330, default_serving_label="банка",
    ),
    BeverageRow(
        "cola_zero", "Кола зеро", "soda", 0.95,
        kcal_per_100ml=0.0, caffeine_mg_per_100ml=10.0,
        aliases=("кола зеро", "coke zero", "diet cola", "cola light"),
        default_serving_ml=330, default_serving_label="банка",
    ),
    BeverageRow(
        "limonad", "Лимонад", "soda", 0.85,
        kcal_per_100ml=40.0, carbs_g_per_100ml=10.0, sugar_g_per_100ml=10.0,
        aliases=("лимонад", "lemonade", "тархун", "дюшес"),
        default_serving_ml=330, default_serving_label="банка",
    ),
    BeverageRow(
        "tonik", "Тоник", "soda", 0.90,
        kcal_per_100ml=34.0, carbs_g_per_100ml=8.8, sugar_g_per_100ml=8.8,
        aliases=("тоник", "tonic", "schweppes", "швепс"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "sprite", "Спрайт", "soda", 0.85,
        kcal_per_100ml=37.0, carbs_g_per_100ml=9.3, sugar_g_per_100ml=9.3,
        aliases=("спрайт", "sprite", "7up", "севен ап"),
        default_serving_ml=330, default_serving_label="банка",
    ),
    BeverageRow(
        "energetik", "Энергетик", "soda", 0.80,
        kcal_per_100ml=45.0, carbs_g_per_100ml=11.0, sugar_g_per_100ml=11.0,
        caffeine_mg_per_100ml=32.0,
        aliases=("энергетик", "energy drink", "redbull", "ред булл", "burn", "monster"),
        default_serving_ml=250, default_serving_label="банка",
    ),

    # --- milk -----------------------------------------------------------
    BeverageRow(
        "moloko_2_5", "Молоко 2.5%", "milk", 1.50,
        kcal_per_100ml=52.0, protein_g_per_100ml=2.9,
        fat_g_per_100ml=2.5, carbs_g_per_100ml=4.7, sugar_g_per_100ml=4.7,
        aliases=("молоко", "milk", "молоко коровье"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "moloko_obezzhirennoye", "Молоко обезжиренное", "milk", 1.50,
        kcal_per_100ml=31.0, protein_g_per_100ml=3.0,
        fat_g_per_100ml=0.1, carbs_g_per_100ml=4.7, sugar_g_per_100ml=4.7,
        aliases=("обезжиренное молоко", "skim milk", "0.5% молоко"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "kefir", "Кефир", "milk", 1.40,
        kcal_per_100ml=40.0, protein_g_per_100ml=2.8,
        fat_g_per_100ml=1.0, carbs_g_per_100ml=4.0, sugar_g_per_100ml=4.0,
        aliases=("кефир", "kefir"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "ryazhenka", "Ряженка", "milk", 1.30,
        kcal_per_100ml=54.0, protein_g_per_100ml=2.9,
        fat_g_per_100ml=2.5, carbs_g_per_100ml=4.2, sugar_g_per_100ml=4.2,
        aliases=("ряженка", "ryazhenka"),
        default_serving_ml=200, default_serving_label="стакан",
    ),
    BeverageRow(
        "yogurt_pityevoy", "Питьевой йогурт", "milk", 1.30,
        kcal_per_100ml=72.0, protein_g_per_100ml=2.8,
        fat_g_per_100ml=1.5, carbs_g_per_100ml=12.0, sugar_g_per_100ml=11.0,
        aliases=("питьевой йогурт", "drinking yogurt", "yogurt drink"),
        default_serving_ml=200, default_serving_label="бутылка",
    ),
    BeverageRow(
        "kakao", "Какао", "milk", 1.25,
        kcal_per_100ml=85.0, protein_g_per_100ml=3.4,
        fat_g_per_100ml=2.6, carbs_g_per_100ml=11.5, sugar_g_per_100ml=10.5,
        caffeine_mg_per_100ml=2.0,
        aliases=("какао", "cocoa", "горячий шоколад", "hot chocolate"),
        default_serving_ml=200, default_serving_label="чашка",
    ),
    BeverageRow(
        "molochnyi_kokteil", "Молочный коктейль", "milk", 1.20,
        kcal_per_100ml=110.0, protein_g_per_100ml=3.5,
        fat_g_per_100ml=3.5, carbs_g_per_100ml=15.0, sugar_g_per_100ml=14.0,
        aliases=("молочный коктейль", "milkshake", "коктейль"),
        default_serving_ml=300, default_serving_label="стакан",
    ),
    BeverageRow(
        "moloko_rastitelnoe_oves", "Овсяное молоко", "milk", 1.20,
        kcal_per_100ml=43.0, protein_g_per_100ml=1.0,
        fat_g_per_100ml=1.5, carbs_g_per_100ml=6.7, sugar_g_per_100ml=4.0,
        aliases=("овсяное молоко", "oat milk"),
        default_serving_ml=200, default_serving_label="стакан",
    ),

    # --- alcohol --------------------------------------------------------
    BeverageRow(
        "pivo", "Пиво (5%)", "alcohol", 0.50,
        kcal_per_100ml=43.0, protein_g_per_100ml=0.5, carbs_g_per_100ml=3.6,
        aliases=("пиво", "beer", "пиво светлое"),
        default_serving_ml=500, default_serving_label="бокал",
    ),
    BeverageRow(
        "pivo_temnoe", "Пиво тёмное", "alcohol", 0.50,
        kcal_per_100ml=53.0, protein_g_per_100ml=0.6, carbs_g_per_100ml=5.0,
        aliases=("темное пиво", "stout", "porter", "стаут"),
        default_serving_ml=500, default_serving_label="бокал",
    ),
    BeverageRow(
        "vino_krasnoe", "Красное вино", "alcohol", 0.20,
        kcal_per_100ml=85.0, carbs_g_per_100ml=2.6, sugar_g_per_100ml=0.6,
        aliases=("красное вино", "red wine", "вино"),
        default_serving_ml=150, default_serving_label="бокал",
    ),
    BeverageRow(
        "vino_beloe", "Белое вино", "alcohol", 0.20,
        kcal_per_100ml=82.0, carbs_g_per_100ml=2.6, sugar_g_per_100ml=1.0,
        aliases=("белое вино", "white wine"),
        default_serving_ml=150, default_serving_label="бокал",
    ),
    BeverageRow(
        "shampanskoe", "Шампанское", "alcohol", 0.20,
        kcal_per_100ml=87.0, carbs_g_per_100ml=3.5, sugar_g_per_100ml=1.5,
        aliases=("шампанское", "champagne", "просекко", "prosecco", "игристое"),
        default_serving_ml=120, default_serving_label="бокал",
    ),
    BeverageRow(
        "vodka", "Водка", "alcohol", -1.00,
        kcal_per_100ml=235.0,
        aliases=("водка", "vodka"),
        default_serving_ml=50, default_serving_label="рюмка",
    ),
    BeverageRow(
        "viski", "Виски", "alcohol", -1.00,
        kcal_per_100ml=250.0,
        aliases=("виски", "whiskey", "whisky", "бурбон", "bourbon"),
        default_serving_ml=50, default_serving_label="рюмка",
    ),
    BeverageRow(
        "konyak", "Коньяк", "alcohol", -1.00,
        kcal_per_100ml=240.0,
        aliases=("коньяк", "cognac", "бренди", "brandy"),
        default_serving_ml=50, default_serving_label="рюмка",
    ),
    BeverageRow(
        "kokteil_alkogolnyi", "Алкогольный коктейль", "alcohol", 0.00,
        kcal_per_100ml=140.0, carbs_g_per_100ml=15.0, sugar_g_per_100ml=14.0,
        aliases=("коктейль алкогольный", "мохито", "mojito", "маргарита", "margarita"),
        default_serving_ml=200, default_serving_label="бокал",
    ),

    # --- broth ----------------------------------------------------------
    BeverageRow(
        "bulyon_kurinyi", "Куриный бульон", "broth", 1.00,
        kcal_per_100ml=15.0, protein_g_per_100ml=1.5,
        fat_g_per_100ml=0.5, carbs_g_per_100ml=1.0,
        aliases=("куриный бульон", "chicken broth", "бульон"),
        default_serving_ml=250, default_serving_label="чашка",
    ),
    BeverageRow(
        "bulyon_govyazhij", "Говяжий бульон", "broth", 1.00,
        kcal_per_100ml=20.0, protein_g_per_100ml=2.0,
        fat_g_per_100ml=1.0, carbs_g_per_100ml=0.5,
        aliases=("говяжий бульон", "beef broth"),
        default_serving_ml=250, default_serving_label="чашка",
    ),
    BeverageRow(
        "bulyon_ovoshnoy", "Овощной бульон", "broth", 1.05,
        kcal_per_100ml=10.0, protein_g_per_100ml=0.5, carbs_g_per_100ml=2.0,
        aliases=("овощной бульон", "vegetable broth"),
        default_serving_ml=250, default_serving_label="чашка",
    ),

    # --- sport / functional --------------------------------------------
    BeverageRow(
        "izotonik", "Изотоник", "sport", 1.10,
        kcal_per_100ml=24.0, carbs_g_per_100ml=6.0, sugar_g_per_100ml=5.0,
        aliases=("изотоник", "isotonic", "powerade", "gatorade"),
        default_serving_ml=500, default_serving_label="бутылка",
    ),
    BeverageRow(
        "elektrolit", "Электролитный напиток", "sport", 1.15,
        kcal_per_100ml=8.0, carbs_g_per_100ml=2.0,
        aliases=("электролит", "electrolyte", "регидрон", "rehydron"),
        default_serving_ml=500, default_serving_label="бутылка",
    ),
    BeverageRow(
        "proteinovyi_kokteil", "Протеиновый коктейль", "sport", 1.20,
        kcal_per_100ml=55.0, protein_g_per_100ml=8.0,
        fat_g_per_100ml=1.0, carbs_g_per_100ml=4.0, sugar_g_per_100ml=2.0,
        aliases=("протеиновый коктейль", "protein shake", "протеин"),
        default_serving_ml=300, default_serving_label="шейкер",
    ),
    BeverageRow(
        "smuzi", "Смузи", "sport", 1.05,
        kcal_per_100ml=58.0, protein_g_per_100ml=1.2,
        fat_g_per_100ml=0.5, carbs_g_per_100ml=13.0, sugar_g_per_100ml=10.0,
        aliases=("смузи", "smoothie"),
        default_serving_ml=300, default_serving_label="стакан",
    ),

    # --- other RU staples ----------------------------------------------
    BeverageRow(
        "mors", "Морс", "juice", 0.95,
        kcal_per_100ml=40.0, carbs_g_per_100ml=9.5, sugar_g_per_100ml=9.0,
        aliases=("морс", "mors", "клюквенный морс", "брусничный морс"),
        default_serving_ml=250, default_serving_label="стакан",
    ),
    BeverageRow(
        "kvas", "Квас", "soda", 0.90,
        kcal_per_100ml=27.0, protein_g_per_100ml=0.2, carbs_g_per_100ml=5.2,
        sugar_g_per_100ml=5.0,
        aliases=("квас", "kvass"),
        default_serving_ml=330, default_serving_label="кружка",
    ),
    BeverageRow(
        "kompot", "Компот", "juice", 0.95,
        kcal_per_100ml=60.0, carbs_g_per_100ml=14.5, sugar_g_per_100ml=14.0,
        aliases=("компот", "compote", "узвар"),
        default_serving_ml=250, default_serving_label="стакан",
    ),
    BeverageRow(
        "limonnaya_voda", "Вода с лимоном", "water", 1.00,
        kcal_per_100ml=4.0, carbs_g_per_100ml=1.0, sugar_g_per_100ml=0.5,
        aliases=("вода с лимоном", "lemon water", "детокс вода"),
        default_serving_ml=300, default_serving_label="стакан",
    ),
)


def beverage_row_to_dict(row: BeverageRow) -> dict:
    """Map a frozen BeverageRow to the kwargs accepted by ``Beverage``."""
    return {
        "name_ru": row.name_ru,
        "category": row.category,
        "water_coefficient": row.water_coefficient,
        "kcal_per_100ml": row.kcal_per_100ml,
        "protein_g_per_100ml": row.protein_g_per_100ml,
        "fat_g_per_100ml": row.fat_g_per_100ml,
        "carbs_g_per_100ml": row.carbs_g_per_100ml,
        "sugar_g_per_100ml": row.sugar_g_per_100ml,
        "caffeine_mg_per_100ml": row.caffeine_mg_per_100ml,
        "aliases": list(row.aliases),
        "default_serving_ml": row.default_serving_ml,
        "default_serving_label": row.default_serving_label,
        "is_active": True,
    }
