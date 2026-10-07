# I01 -- rules and where they come from

**Event:** Gummosis; sooty mould; twig blight  
**Reported:** Gummosis 41.6% (18-66%), sooty mould 31.1%, twig blight 30.7% plant infection; incidence highest in January  
**Source of the event:** Thakre & Sharma (2022) J. Plant Dev. Sci. 14(7):631-634 (JNKVV ZARS Chhindwara) (https://jpds.co.in/wp-content/uploads/2022/08/4.Bhupendra-Thakre-22391.pdf)  
**Incident period:** 2020-07-01 to 2021-06-30; checked from 2020-06-16 (15 days lead-in)  
**Grid cell:** 21.6_78.6 (ERA5-Land 0.1°, nearest cell centre)

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

## P01 -- Citrus psylla - conducive temperature

**From the workbook**

- Rule set: ICAR rule
- Condition: Daily mean temperature 25-30 °C
- Alert levels: 7-day index = days of the last 7 meeting the condition: High >= 5, Moderate >= 3, Low >= 1
- Source: Rao C.N. & Shivankar V.J. (2011) Relative efficacy of certain bio-rational insecticides to citrus psylla. Indian Journal of Agricultural Sciences 81(7):673-676. National Research Centre for Citrus (now ICAR-CCRI), Nagpur, p. 1
- Quotation: "It is active during spring and in dry spells during monsoon (Shivankar et al. 2001). High humidity associated with dry spells and moderate temperatures (25–30°C) are congenial for its rapid development."
- Evidence grade: B
- Notes / caveats: Source gives 25-30 °C without the statistic; daily MEAN used, consistent with the constant-temperature laboratory studies behind such ranges (Liu & Tsai 2000: optimum 25-28 °C; development fails at 33 °C). The band is an optimum, not an upper limit: oviposition continues to 41.6 °C and peaks at 29.6 °C (Hall et al. 2011), so the rule switches off while psylla can still build up. Its 'high humidity' and 'dry spells' qualifiers have no numbers and are not applied.

**How it is computed**

Daily mean of hourly 2 m temperature between 25 and 30 °C inclusive.

## G-P01 -- Asian Citrus Psylla (Diaphorina citri) - new-adult emergence window

**From the workbook**

- Rule set: International literature (degree-day model)
- Condition: Daily degree-days above 10.5 °C with upper cut-off 33.0 °C, accumulated from 1 January; 250.0 DD per generation; flag = the last 63.3 DD of each generation (adult pre-oviposition / new-adult emergence window)
- Alert levels: 7-day index = days of the last 7 meeting the condition: High >= 5, Moderate >= 3, Low >= 1
- Source: Liu & Tsai (2000) Ann. Appl. Biol. 137:201-206, Table 3: 250 DD egg-to-adult above 10.5 °C; 5th instar 63.3 DD; 'The populations reared at 10°C and 33°C failed to develop'
- Notes / caveats: Laboratory constant temperatures (Florida colony, orange jessamine). The 5th-instar DD is estimated above its own threshold (10.9 °C), within 0.4 °C of the whole-life threshold used here.

**How it is computed**

Daily degree-days = mean over the 24 hours of (hourly temperature capped to 10.5-33.0 °C minus 10.5) -- horizontal cut-off. Accumulated from 1 January; flag when the running total is in the last 63.3 DD of a 250 DD generation.

## G-P03 -- Citrus Mealybug (Planococcus citri) - new-adult emergence window

**From the workbook**

- Rule set: International literature (degree-day model)
- Condition: Daily degree-days above 8.5 °C with upper cut-off 27.5 °C, accumulated from 1 January; 666.67 DD per generation; flag = the last 182.3 DD of each generation (adult pre-oviposition / new-adult emergence window)
- Alert levels: 7-day index = days of the last 7 meeting the condition: High >= 5, Moderate >= 3, Low >= 1
- Source: Karacaoglu & Satar (2017) Turk. J. Entomol. 41(2):147-157 (grapefruit leaves): female threshold 8.5 °C and thermal constant 666.67 DD; 'optimum development temperature ... 25/30°C' (mean 27.5 °C, used as the cut-off); no development at 35 °C; third nymph 7.0 of 25.6 d at 25 °C (27%), i.e. 182.3 DD
- Notes / caveats: Female development (the reproductive sex). Development at 30 °C was slower than at 25 °C, so the cut-off is set at the reported optimum rather than the highest temperature with development.

**How it is computed**

As G-P01 with 8.5 °C base, 27.5 °C cut-off, 666.67 DD per generation, flag = last 182.3 DD.

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

## T13 -- Citrus blackfly

**From the workbook**

- Rule set: Season advisory (calendar)
- Condition: Active in: Apr, Aug, Dec. Normally active April, August, December; ETL 5-10 nymphs/leaf (ICAR-CCRI advisory 2022)
- Alert levels: Active / not active
- Source: ICAR-CCRI Blackfly advisory, 21 November 2022 (Marathi), p. 1-2
- Quotation: "साधारणता एप्रिल, ऑगस्ट व डिसेंबर महिन्यामध्ये सक्रिय असणारी काळी माशी ... [Blackfly, normally active in April, August and December, has appeared in very large numbers in November.] ETL 5-10 nymphs/leaf"

**How it is computed**

Calendar only: active in months [4, 8, 12]. No weather data.
