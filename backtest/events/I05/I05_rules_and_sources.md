# I05 -- rules and where they come from

**Event:** Phytophthora brown rot  
**Reported:** 'Incidence of Phytophthora brown rot disease has started appearing' in Katol and Narkhed  
**Source of the event:** ICAR-CCRI Disease Occurrence Alert (5 Jul 2021) (https://ccri.org.in/site/assets/files/1167/icar_ccri_phytopthora_brown_rot_advisory_english_july_2021.pdf)  
**Incident period:** 2021-07-05 to 2021-07-05; checked from 2021-06-20 (15 days lead-in)  
**Grid cell:** 21.5_78.5 (ERA5-Land 0.1°, nearest cell centre)

Everything under *From the workbook* is copied from sheet 2 of `280926-Pandhurna-Backtest- V2.xlsx`. *How it is computed* is our implementation of that wording; any choice the sheet does not fix is stated there.

General choices for every rule:

- Days are Indian calendar days (IST, UTC+5:30), built from ERA5-Land hourly data.
- 7-day index = number of the last 7 days (including today) meeting the condition: High >= 5, Moderate >= 3, Low >= 1.
- The alert columns in the sheet (S, T, U) count weather rules only; calendar rules are reported in the remarks.

## G-D02 -- Phytophthora brown rot - sporulation and infection day

**From the workbook**

- Rule set: International literature (infection model)
- Condition: Leaf wetness >= 18 h with daily mean temperature > 22 °C, July-October
- Alert levels: 7-day index = days of the last 7 meeting the condition: High >= 5, Moderate >= 3, Low >= 1
- Source: Timmer, Zitko, Gottwald & Graham (2000) Plant Dis. 84:157-163 (P. palmivora, P. nicotianae): 'A few sporangia were produced with 18 h of fruit wetness, and numbers increased as duration of wetness increased up to 72 h'; 'No brown rot developed at 22°C or less'; epidemics 'from July to October during the rainy season'. ICAR-CCRI (2021) brown-rot alert: 'continuous wet weather', first incidence reported 5 July 2021
- Notes / caveats: 18 h is the minimum wetness for sporangium production; infection itself needs only 3 h, so the rule marks days when inoculum can build up. Fruit wetness is approximated by the leaf-wetness proxy.

**How it is computed**

Leaf-wetness hours = hours with RH >= 90 % (RH from 2 m temperature and dewpoint, Magnus form as in pipeline.py); day passes if >= 18 wet hours and daily mean temperature > 22 °C, only July-October.

## X-W01 -- Soil waterlogging - Phytophthora root rot / gummosis risk

**From the workbook**

- Rule set: Next-step rule
- Condition: Root zone at field capacity with rain >= crop ET for >= 3 consecutive days (72 h), FAO-56 water balance for citrus on clay
- Alert levels: Event rule: levels as stated in the condition
- Source: UF/IFAS (Central Florida Ag News, 9 Nov 2022): 'standing water that has remained for longer than 72 hours can cause acute root damage that makes citrus trees much more susceptible to phytophthora infection'. Water balance: FAO-56 (Allen et al. 1998) Eqs. 52, 82, 85, 88; Tables 12, 19, 22.
- Notes / caveats: Soil saturation is modelled from rain and temperature, not measured; drainage and irrigation of the individual orchard are not represented.

**How it is computed**

Daily FAO-56 root-zone water balance (Eq. 85, no irrigation, no runoff): depletion Dr starts at TAW on the first day of data (dry season) and each day Dr = Dr - rain + ETc, bounded to 0..TAW; Dr = 0 is field capacity (surplus drains). ETc = Ks x Kc x ET0 (Eq. 84 stress coefficient). ET0 = FAO-56 Penman-Monteith (Eq. 6) from daily Tmax/Tmin, mean dewpoint, 10 m wind converted to 2 m (Eq. 47), solar radiation and surface pressure. Kc = 0.7 (Table 12), root depth 1.35 m and p = 0.5 (Table 22), clay FC 0.36 / WP 0.22 (Table 19) -> TAW = 189 mm. Alert on every day that completes a run of >= 3 consecutive days with the root zone at field capacity and rain >= ETc.

## T09 -- Phytophthora brown rot

**From the workbook**

- Rule set: Season advisory (calendar)
- Condition: Active in: Aug, Sep, Oct (from 08-15). Late monsoon mid-August to October after continuous wet weather (ICAR-CCRI alert 2021)
- Alert levels: Active / not active
- Source: ICAR-CCRI Disease Occurrence Alert for Phytophthora brown rot, 5 July 2021, p. 1
- Quotation: "Phytophthora brown rot is a fruit disease usually associated with continuous wet weather and poor water drainage conditions. It commonly appears during late monsoon phase (mid-August to October) following periods of extended high rainfall."

**How it is computed**

Calendar only: active in months [8, 9, 10] from 08-15. No weather data.

## X-B01 -- Brown-rot early-onset watch

**From the workbook**

- Rule set: Season advisory (calendar)
- Condition: Active in: Jul, Aug (from 07-01). Brown rot can start in early July: ICAR-CCRI Disease Occurrence Alert of 5 July 2021 ('incidence of Phytophthora brown rot disease has started appearing' in Katol and Narkhed), ahead of the usual mid-August onset (T09); Timmer et al. 2000: epidemics from July to October.
- Alert levels: Active / not active
- Source: see condition

**How it is computed**

Calendar only: active in months [7, 8] from 07-01. No weather data.
