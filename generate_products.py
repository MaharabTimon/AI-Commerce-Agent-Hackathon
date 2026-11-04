import json
import random

categories = {
    "Smartphones": {
        "brands": ["Apple", "Samsung", "Google Pixel", "Xiaomi", "OnePlus", "Nothing", "Vivo", "Oppo", "Realme", "Sony", "Asus ROG"],
        "models": ["Pro", "Pro Max", "Ultra", "Plus", "Lite", "Neo", "Edge", "Fold", "Flip", "GT", "Prime", "SE", "Zoom"],
        "specs_templates": [
            "Processor: {cpu}, RAM: {ram}GB, Storage: {storage}GB, Display: {display}\" AMOLED 120Hz, Battery: {battery}mAh with {charge}W fast charging, Main Camera: {cam}MP Triple Camera setup, OS: Android 14 / iOS.",
            "Display: {display}\" Super Retina OLED, Chipset: {cpu}, Storage: {storage}GB NVMe, Camera: {cam}MP with Optical Image Stabilization, Battery: {battery}mAh, 5G Dual SIM support, IP68 water resistance.",
            "Screen: {display}\" 144Hz HDR10+ Display, CPU: {cpu}, {ram}GB LPDDR5X RAM, {storage}GB UFS 4.0 Storage, {battery}mAh battery, {charge}W SuperVOOC charging, Stereo Dolby Atmos speakers."
        ],
        "cpus": ["Snapdragon 8 Gen 3", "Snapdragon 8 Gen 2", "Snapdragon 7+ Gen 3", "Dimensity 9300", "Dimensity 8200", "Apple A17 Pro", "Apple A16 Bionic", "Google Tensor G3"],
        "price_range": (25000, 195000)
    },
    "Laptops": {
        "brands": ["Apple MacBook", "Dell XPS", "Lenovo ThinkPad", "Lenovo Legion", "HP Spectre", "HP Envy", "Asus ZenBook", "Asus ROG Zephyrus", "Acer Swift", "Razer Blade", "MSI Stealth"],
        "models": ["13", "14", "15", "16", "Pro", "Air", "Carbon Gen 11", "Studio", "Ultra 7", "Ultra 9", "Evo"],
        "specs_templates": [
            "Processor: {cpu}, RAM: {ram}GB DDR5, Storage: {storage}GB NVMe PCIe Gen 4 SSD, Display: {display}\" 2.8K OLED 120Hz, Graphics: {gpu}, Battery: {battery}Wh (up to 12 hours), Weight: 1.35 kg.",
            "CPU: {cpu}, Memory: {ram}GB Unified Memory, SSD: {storage}GB, Display: {display}\" Liquid Retina XDR, Backlit Magic Keyboard with Touch ID, Battery life: up to 18 hours, Thunderbolt 4 ports.",
            "Processor: {cpu}, GPU: {gpu}, RAM: {ram}GB 5600MHz, Storage: {storage}TB SSD, Screen: {display}\" QHD+ 240Hz Gaming Panel, Vapor Chamber Cooling, RGB Keyboard."
        ],
        "cpus": ["Intel Core Ultra 7 155H", "Intel Core i9-14900HX", "AMD Ryzen 7 8845HS", "AMD Ryzen 9 7940HS", "Apple M3 Pro", "Apple M3 Max", "Intel Core i7-13700H"],
        "gpus": ["NVIDIA RTX 4070 8GB", "NVIDIA RTX 4060 8GB", "NVIDIA RTX 4080 12GB", "Intel Arc Graphics", "Apple Integrated 18-Core GPU"],
        "price_range": (65000, 360000)
    },
    "Audio & Headphones": {
        "brands": ["Sony", "Bose", "Sennheiser", "Apple AirPods", "JBL", "Marshall", "Audio-Technica", "Beyerdynamic", "Anker Soundcore", "Nothing Ear"],
        "models": ["QuietComfort", "WH-1000XM", "Momentum", "Major IV", "Tune", "Life Q", "Pro Wireless", "Elite ANC", "Studio Wireless", "OpenComm"],
        "specs_templates": [
            "Active Noise Cancellation (ANC) with transparency mode, 40mm Neodymium dynamic drivers, Battery life: {battery} hours (ANC on), Bluetooth 5.3 with LDAC/AAC support, Multi-point connection, Fast USB-C charging.",
            "True Wireless Stereo (TWS) Earbuds, Adaptive ANC, Spatial Audio with dynamic head tracking, Up to {battery} hours battery with wireless charging case, IPX4 sweat resistance, Quad beamforming mics.",
            "Over-ear Hi-Res audio headphones, Custom-tuned drivers, 30+ hours playtime, Vintage leatherette finish, Bluetooth 5.2 and 3.5mm wired input, Built-in microphone."
        ],
        "price_range": (3500, 52000)
    },
    "Smartwatches & Fitness": {
        "brands": ["Apple Watch", "Samsung Galaxy Watch", "Garmin", "Amazfit", "Fitbit", "Huawei Watch", "OnePlus Watch"],
        "models": ["Ultra 2", "Series 9", "Classic 6", "Forerunner 965", "Fenix 7 Pro", "GTR 4", "Charge 6", "Fit 3", "Active Edge"],
        "specs_templates": [
            "Display: 1.9\" Always-On AMOLED Display (2000 nits), ECG and Blood Oxygen (SpO2) monitor, Dual-frequency GPS, 50m water resistant, Battery life: {battery} days, Titanium/Aluminum case.",
            "AMOLED touch display, Advanced sleep coaching, Body composition BIA sensor, Heart rate monitoring, NFC for contactless payments, WearOS with Google Assistant, 5ATM + IP68 rating.",
            "Multi-sport fitness smartwatch, Topographic maps, Heart rate & HRV status tracking, Solar charging support, Up to {battery} days battery in smartwatch mode, Military-grade durability (MIL-STD-810H)."
        ],
        "price_range": (4500, 115000)
    },
    "Cameras & Photography": {
        "brands": ["Sony Alpha", "Canon EOS", "Nikon Z", "Fujifilm", "Panasonic Lumix", "GoPro", "DJI Osmo"],
        "models": ["A7 IV", "A6700", "R6 Mark II", "R8", "Z6 III", "X-T5", "X-S20", "GH6", "Hero 12 Black", "Pocket 3", "Action 4"],
        "specs_templates": [
            "Sensor: 33MP Full-Frame Exmor R CMOS Sensor, 4K 60p 10-bit 4:2:2 video, 759-point Phase-detection AF with Real-time Eye AF, 5-axis in-body image stabilization (IBIS), Dual card slots.",
            "26.1MP APS-C X-Trans CMOS 4 sensor, Classic film simulation modes, 6.2K 30p internal recording, 5-axis IBIS up to 7 stops, Vari-angle touchscreen LCD, Compact weather-sealed body.",
            "Compact 4K 120fps stabilization camera, 1-inch CMOS sensor, 3-axis mechanical gimbal, 2-inch rotatable OLED touchscreen, Stereo sound recording, ActiveTrack 6.0 subject tracking."
        ],
        "price_range": (32000, 290000)
    },
    "PC Components & Gaming": {
        "brands": ["NVIDIA GeForce", "AMD Radeon", "Intel Core", "AMD Ryzen", "Corsair Vengeance", "Samsung", "Kingston Fury", "ASUS ROG Strix", "MSI Gaming X", "Gigabyte AORUS"],
        "models": ["RTX 4090 24GB", "RTX 4080 Super 16GB", "RTX 4070 Ti Super", "RTX 4060 Ti 8GB", "RX 7900 XTX 24GB", "RX 7800 XT 16GB", "i9-14900K", "Ryzen 7 7800X3D", "Ryzen 9 7950X", "990 PRO 2TB PCIe 4.0 SSD", "32GB DDR5 6000MHz RAM"],
        "specs_templates": [
            "Architecture: Ada Lovelace / RDNA 3, VRAM: 16GB/24GB GDDR6X, Ray Tracing 3rd Gen, DLSS 3.5 / FSR 3 frame generation, Triple axial-tech cooling fans, Dual BIOS support, DisplayPort 1.4a & HDMI 2.1.",
            "Cores: 8 Cores / 16 Threads, Base Clock: 4.2GHz, Boost Clock: up to 5.0GHz, 96MB 3D V-Cache, AM5 Socket, 120W TDP, Optimized for ultra-high FPS competitive PC gaming.",
            "Interface: PCIe Gen 4.0 x4, NVMe 2.0, Read Speed: up to 7,450 MB/s, Write Speed: up to 6,900 MB/s, Nickel-coated controller for thermal management, 5-year warranty."
        ],
        "price_range": (12000, 240000)
    },
    "Monitors & Displays": {
        "brands": ["LG UltraGear", "Samsung Odyssey", "Dell UltraSharp", "ASUS TUF Gaming", "BenQ Mobiuz", "AOC Gaming", "Gigabyte", "ViewSonic ColorPro"],
        "models": ["OLED 27\"", "G7 32\"", "G9 Neo 49\" Curved", "U2723QE 4K", "VG27AQ 27\"", "EX2710Q", "M28U 4K 144Hz", "VP2776 Pro 100% sRGB"],
        "specs_templates": [
            "Panel: 27-inch QHD (2560x1440) OLED, Refresh Rate: 240Hz, Response Time: 0.03ms GtG, HDR10 with 98.5% DCI-P3 color gamut, NVIDIA G-Sync Compatible & AMD FreeSync Premium Pro, HDMI 2.1 & DP 1.4.",
            "Panel: 27-inch 4K UHD (3840x2160) IPS Black Technology, 2000:1 contrast ratio, 100% sRGB & 98% DCI-P3, USB-C Hub with 90W Power Delivery, RJ45 Ethernet, Height/Tilt/Swivel/Pivot adjustable stand.",
            "Panel: 34-inch WQHD (3440x1440) 1000R Ultrawide Curved VA, 165Hz Refresh Rate, 1ms MPRT, HDR400, Built-in 2x 5W stereo speakers, Picture-in-Picture (PiP) multitasking."
        ],
        "price_range": (22000, 165000)
    },
    "Tablets": {
        "brands": ["Apple iPad", "Samsung Galaxy Tab", "Xiaomi Pad", "Lenovo Tab", "OnePlus Pad", "Microsoft Surface"],
        "models": ["Pro 11\" M4", "Pro 13\" M4", "Air 11\" M2", "S9 Ultra", "S9+", "S9 FE", "6 Pro", "P12 Pro", "Go 4", "Pro 9"],
        "specs_templates": [
            "Display: Ultra Retina XDR Tandem OLED, Chipset: Apple M4 Chip with 9-core CPU & 10-core GPU, Storage: 256GB/512GB, Apple Pencil Pro support, Face ID, Four studio-quality microphones, Wi-Fi 6E.",
            "Display: 14.6-inch Dynamic AMOLED 2X 120Hz, Snapdragon 8 Gen 2 for Galaxy, 12GB RAM, S-Pen included with ultra-low latency, IP68 water & dust resistance, 11,200mAh battery with 45W fast charge.",
            "Screen: 11.61-inch 144Hz 7:5 ReadFit display, Dimensity 9000 flagship processor, 8GB RAM, 9510mAh battery with 67W SuperVOOC, Quad-speakers with Dolby Vision Atmos."
        ],
        "price_range": (28000, 185000)
    },
    "Accessories & Peripherals": {
        "brands": ["Logitech", "Keychron", "Razer", "SteelSeries", "Anker", "Ugreen", "Belkin", "HyperX"],
        "models": ["MX Master 3S", "MX Keys Mini", "Q1 Pro Wireless", "V3 Max Mechanical", "DeathAdder V3 Pro", "Apex Pro TKL", "Prime 27,650mAh Power Bank", "Revodok 9-in-1 Hub", "Cloud III Wireless"],
        "specs_templates": [
            "8,000 DPI Darkfield sensor (tracks on glass), Quiet Click technology, MagSpeed electromagnetic scroll wheel, Bluetooth + Logi Bolt USB, USB-C fast charging (up to 70 days battery life).",
            "75% QMK/VIA wireless custom mechanical keyboard, Hot-swappable Keychron K Pro switches, Double-gasket mount design, Bluetooth 5.1 & Type-C wired, CNC Aluminum frame, Mac & Windows compatible.",
            "Output: 250W total output with dual USB-C (140W each), 27,650mAh capacity (TSA approved), Smart digital display with real-time wattage tracking, Anker app connectivity, Charges MacBook Pro 16 to 50% in 28 mins."
        ],
        "price_range": (2500, 32000)
    },
    "Smart Home & Appliances": {
        "brands": ["Dyson", "Roborock", "Philips Hue", "Xiaomi", "Ecovacs", "Google Nest", "Amazon Echo"],
        "models": ["V12 Detect Slim", "V15 Detect Absolute", "S8 MaxV Ultra", "Q Revo", "Deebot T20 Omni", "Smart Air Purifier 4 Pro", "Hub 2nd Gen", "Show 10"],
        "specs_templates": [
            "Laser Slim Fluffy cleaner head illuminates microscopic dust, Piezo sensor measures and counts dust particles, Up to 60 minutes run time, LCD screen displays run time and particle count, Whole-machine HEPA filtration.",
            "10,000 Pa Extreme Suction, All-in-One RockDock with auto-emptying, auto-mop washing with hot water, and heated air drying, Reactive AI 2.0 obstacle avoidance, LiDAR navigation.",
            "CADR of up to 500m³/h, 3-in-1 high-efficiency filter captures 99.97% of 0.3μm particles, OLED touch display, Voice control via Google Assistant & Alexa, Low-noise operation (33.7 dB in night mode)."
        ],
        "price_range": (6000, 145000)
    }
}

products = []
product_id = 1
seen_names = set()

# Deterministic seed for reproducible quality data
random.seed(42)

for category_name, data in categories.items():
    brands = data["brands"]
    models = data["models"]
    specs_templates = data["specs_templates"]
    price_min, price_max = data["price_range"]
    cpus = data.get("cpus", ["Octa-Core High Performance CPU", "Next-Gen AI Neural Engine"])
    gpus = data.get("gpus", ["Dedicated High-End GPU", "Integrated Neural Graphics"])

    target_per_category = 100  # 10 categories * 100 = 1000 items

    cat_count = 0
    attempts = 0
    while cat_count < target_per_category and attempts < 2000:
        attempts += 1
        brand = random.choice(brands)
        model = random.choice(models)
        
        # Suffix / Edition to make 1000 distinct, unique names
        edition = random.choice([
            "Edition", "Special Edition", "2024 Edition", "2025 Edition", "Plus", "Max", 
            "Ultra", "Standard", "Elite", "Series X", "Enterprise", "Signature", 
            "Limited Black", "Arctic White", "Midnight", "Space Gray", "Carbon", 
            "5G", "Wi-Fi", "v2", "Refreshed", "Gen 2", "Gen 3", "Slim"
        ])
        storage_opt = random.choice([128, 256, 512, 1024])
        ram_opt = random.choice([8, 12, 16, 24, 32, 64])

        name = f"{brand} {model} {edition}"
        if name in seen_names:
            name = f"{brand} {model} {edition} ({storage_opt}GB)"
        if name in seen_names:
            name = f"{brand} {model} {edition} #{cat_count + 1}"
        
        seen_names.add(name)

        # Price calculation with realistic rounding
        base_price = random.randint(price_min, price_max)
        price_bdt = round(base_price, -2)  # round to nearest hundred

        # Specifications interpolation
        tmpl = random.choice(specs_templates)
        spec = tmpl.format(
            cpu=random.choice(cpus),
            gpu=random.choice(gpus),
            ram=ram_opt,
            storage=storage_opt,
            display=random.choice([6.1, 6.7, 6.8, 13.3, 14.0, 15.6, 16.0]),
            battery=random.choice([4500, 5000, 5400, 70, 85, 99, 40, 50]),
            charge=random.choice([33, 67, 80, 100, 120]),
            cam=random.choice([50, 64, 108, 200])
        )

        products.append({
            "id": product_id,
            "name": name,
            "category": category_name,
            "brand": brand,
            "price_bdt": price_bdt,
            "price_formatted": f"BDT {price_bdt:,}",
            "specifications": spec,
            "in_stock": random.choice([True, True, True, False])  # 75% in stock
        })
        product_id += 1
        cat_count += 1

# Ensure exactly 1000 items
products = products[:1000]

with open("products.json", "w", encoding="utf-8") as f:
    json.dump(products, f, indent=2, ensure_ascii=False)

print(f"Successfully generated {len(products)} unique products in products.json")
