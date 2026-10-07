# I28 -- rules and where they come from

**Event:** Fruit drop of sweet orange (same season as the Katol brown-rot outbreak, I23)  
**Reported:** ICAR-CCRI tour 'to Jarud, Warud, Bargoan and Gadegoan area of Amravati district to assess fruit drop problems of Sweet orange on 5th September, 2020'  
**Source of the event:** ICAR-CCRI Citrus Newsletter Jul-Sep 2020, p.7 (https://ccri.org.in/site/assets/files/2024/july-sep2020.pdf)  
**Incident period:** 2020-08-06 to 2020-09-05; checked from 2020-07-22 (15 days lead-in)  
**Grid cell:** 21.5_78.4 (ERA5-Land 0.1°, nearest cell centre)

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

## G-D01 -- Alternaria Brown Spot (Alternaria alternata) - high-risk day

**From the workbook**

- Rule set: International literature (infection model)
- Condition: Rain day (>= 2.5 mm) or leaf wetness > 10 h, with daily mean temperature 20-28 °C
- Alert levels: 7-day index = days of the last 7 meeting the condition: High >= 5, Moderate >= 3, Low >= 1
- Source: Timmer et al. (2000) Plant Dis. 84:638-643 (ALTER-RATER): days classed by rain vs no rain, < or > 10 h leaf wetness and < 20, 20-28, > 28 °C; severity nearly doubled on rain days, 'considerable infection occurred on days with >10 h leaf wetness duration and no rain', and 'Infection was greatest on days with temperatures of 20 to 28°C'. Canihos et al. (1999) Plant Dis. 83:429-433: infection greatest at 27 °C, low at 4-8 h wetness
- Notes / caveats: Uses the highest-risk ALTER-RATER classes as a daily flag instead of its cumulative points (the point table is in the paywalled full text). Rain day uses the IMD 2.5 mm definition.

**How it is computed**

Day passes if (daily rain >= 2.5 mm, IMD rain day, or leaf-wetness hours > 10, RH >= 90 % proxy) and daily mean temperature is 20-28 °C inclusive.

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
