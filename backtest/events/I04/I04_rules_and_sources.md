# I04 -- rules and where they come from

**Event:** Fruit rot on tree; Mrig bahar failure; gummosis increasing  
**Reported:** Continuous rain; Mrig bahar crop failed; gummosis 'increased in the last few years' (ZARS scientist)  
**Source of the event:** Ground Report (24 Feb 2025) (https://www.groundreport.in/groundreport/erratic-rainfall-orange-farmers-and-traders-suffer-in-pandhura-8751469/)  
**Incident period:** 2024-06-01 to 2024-10-31; checked from 2024-05-17 (15 days lead-in)  
**Grid cell:** 21.6_78.5 (ERA5-Land 0.1°, nearest cell centre)

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

## T09 -- Phytophthora brown rot

**From the workbook**

- Rule set: Season advisory (calendar)
- Condition: Active in: Aug, Sep, Oct (from 08-15). Late monsoon mid-August to October after continuous wet weather (ICAR-CCRI alert 2021)
- Alert levels: Active / not active
- Source: ICAR-CCRI Disease Occurrence Alert for Phytophthora brown rot, 5 July 2021, p. 1
- Quotation: "Phytophthora brown rot is a fruit disease usually associated with continuous wet weather and poor water drainage conditions. It commonly appears during late monsoon phase (mid-August to October) following periods of extended high rainfall."

**How it is computed**

Calendar only: active in months [8, 9, 10] from 08-15. No weather data.

## A05 -- Waterlogging risk (monsoon)

**From the workbook**

- Rule set: Season advisory (calendar)
- Condition: Active in: Jun, Jul, Aug, Sep. Continuous high rainfall in a short span leading to waterlogging (ICAR-CRIDA Contingency Plan; the plan gives no months - June-September taken from the kharif crop stages it lists)
- Alert levels: Active / not active
- Source: ICAR-CRIDA (2013) Contingency Plan Chhindwara, section 2.2, p. 17
- Quotation: "Continuous high rainfall in a short span leading to water logging ... Heavy rainfall with high speed wind in a short span"

**How it is computed**

Calendar only: active in months [6, 7, 8, 9]. No weather data.

## X-W01 -- Soil waterlogging - Phytophthora root rot / gummosis risk

**From the workbook**

- Rule set: Next-step rule
- Condition: Root zone at field capacity with rain >= crop ET for >= 3 consecutive days (72 h), FAO-56 water balance for citrus on clay
- Alert levels: Event rule: levels as stated in the condition
- Source: UF/IFAS (Central Florida Ag News, 9 Nov 2022): 'standing water that has remained for longer than 72 hours can cause acute root damage that makes citrus trees much more susceptible to phytophthora infection'. Water balance: FAO-56 (Allen et al. 1998) Eqs. 52, 82, 85, 88; Tables 12, 19, 22.
- Notes / caveats: Soil saturation is modelled from rain and temperature, not measured; drainage and irrigation of the individual orchard are not represented.

**How it is computed**

Daily FAO-56 root-zone water balance (Eq. 85, no irrigation, no runoff): depletion Dr starts at TAW on the first day of data (dry season) and each day Dr = Dr - rain + ETc, bounded to 0..TAW; Dr = 0 is field capacity (surplus drains). ETc = Ks x Kc x ET0 (Eq. 84 stress coefficient). ET0 = FAO-56 Penman-Monteith (Eq. 6) from daily Tmax/Tmin, mean dewpoint, 10 m wind converted to 2 m (Eq. 47), solar radiation and surface pressure. Kc = 0.7 (Table 12), root depth 1.35 m and p = 0.5 (Table 22), clay FC 0.36 / WP 0.22 (Table 19) -> TAW = 189 mm. Alert on every day that completes a run of >= 3 consecutive days with the root zone at field capacity and rain >= ETc.
