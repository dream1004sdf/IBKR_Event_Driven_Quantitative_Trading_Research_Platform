from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MacroSeries:
    series_id: str
    label: str
    region: str
    category: str
    source_family: str
    importance: int = 3
    freshness_note: str = ""


FRED_GLOBAL_CORE_SERIES: tuple[MacroSeries, ...] = (
    MacroSeries("FEDFUNDS", "Federal funds effective rate", "US", "policy_rates", "Federal Reserve", 5),
    MacroSeries("SOFR", "Secured Overnight Financing Rate", "US", "policy_rates", "Federal Reserve", 5),
    MacroSeries("EFFR", "Effective Federal Funds Rate", "US", "policy_rates", "Federal Reserve", 5),
    MacroSeries("DGS3MO", "US Treasury 3-month yield", "US", "rates_curve", "US Treasury/FRED", 5),
    MacroSeries("DGS2", "US Treasury 2-year yield", "US", "rates_curve", "US Treasury/FRED", 5),
    MacroSeries("DGS10", "US Treasury 10-year yield", "US", "rates_curve", "US Treasury/FRED", 5),
    MacroSeries("DGS30", "US Treasury 30-year yield", "US", "rates_curve", "US Treasury/FRED", 4),
    MacroSeries("T10Y2Y", "US 10Y minus 2Y Treasury spread", "US", "rates_curve", "FRED", 5),
    MacroSeries("T10Y3M", "US 10Y minus 3M Treasury spread", "US", "rates_curve", "FRED", 5),
    MacroSeries("MORTGAGE30US", "US 30-year fixed mortgage rate", "US", "housing_credit", "Freddie Mac/FRED", 4),
    MacroSeries("BAMLH0A0HYM2", "US high-yield option-adjusted spread", "US", "credit_risk", "ICE BofA/FRED", 5),
    MacroSeries("BAMLC0A0CM", "US corporate option-adjusted spread", "US", "credit_risk", "ICE BofA/FRED", 4),
    MacroSeries("VIXCLS", "CBOE VIX close", "US", "volatility", "CBOE/FRED", 5),
    MacroSeries("WALCL", "Federal Reserve total assets", "US", "liquidity", "Federal Reserve", 4),
    MacroSeries("RRPONTSYD", "Fed overnight reverse repo operations", "US", "liquidity", "Federal Reserve", 4),
    MacroSeries("M2SL", "US M2 money stock", "US", "liquidity", "Federal Reserve", 3),
    MacroSeries("BUSLOANS", "Commercial and industrial loans", "US", "credit", "Federal Reserve", 3),
    MacroSeries("TOTLL", "Loans and leases in bank credit", "US", "credit", "Federal Reserve", 3),
    MacroSeries("DTWEXBGS", "Nominal broad US dollar index", "Global", "fx_dollar", "Federal Reserve", 5),
    MacroSeries("DEXUSEU", "US dollar to euro spot exchange rate", "Europe", "fx", "Federal Reserve", 4),
    MacroSeries("DEXJPUS", "Japanese yen to US dollar spot exchange rate", "Japan", "fx", "Federal Reserve", 4),
    MacroSeries("DEXCHUS", "Chinese yuan to US dollar spot exchange rate", "China", "fx", "Federal Reserve", 4),
    MacroSeries("DEXKOUS", "Korean won to US dollar spot exchange rate", "Korea", "fx", "Federal Reserve", 4),
    MacroSeries("DCOILWTICO", "WTI crude oil price", "Global", "commodities_energy", "EIA/FRED", 5),
    MacroSeries("DCOILBRENTEU", "Brent crude oil price", "Global", "commodities_energy", "EIA/FRED", 5),
    MacroSeries("CPIAUCSL", "US CPI all items", "US", "inflation", "BLS", 5),
    MacroSeries("CPILFESL", "US core CPI", "US", "inflation", "BLS", 5),
    MacroSeries("PPIACO", "US PPI all commodities", "US", "inflation", "BLS", 4),
    MacroSeries("PCEPI", "US PCE price index", "US", "inflation", "BEA", 5),
    MacroSeries("PCEPILFE", "US core PCE price index", "US", "inflation", "BEA", 5),
    MacroSeries("PCE", "US personal consumption expenditures", "US", "consumption", "BEA", 4),
    MacroSeries("RSAFS", "US advance retail sales", "US", "consumption", "Census/FRED", 5),
    MacroSeries("RSXFS", "US advance retail sales excluding food services", "US", "consumption", "Census/FRED", 4),
    MacroSeries("PAYEMS", "US nonfarm payrolls", "US", "labor", "BLS", 5),
    MacroSeries("UNRATE", "US unemployment rate", "US", "labor", "BLS", 5),
    MacroSeries("ICSA", "US initial jobless claims", "US", "labor_high_frequency", "DOL/FRED", 5),
    MacroSeries("IC4WSA", "US initial claims 4-week average", "US", "labor_high_frequency", "DOL/FRED", 4),
    MacroSeries("CCSA", "US continued claims", "US", "labor_high_frequency", "DOL/FRED", 4),
    MacroSeries("JTSJOL", "US JOLTS job openings", "US", "labor", "BLS", 4),
    MacroSeries("JTSHIR", "US JOLTS hires", "US", "labor", "BLS", 3),
    MacroSeries("JTSQUR", "US JOLTS quits", "US", "labor", "BLS", 3),
    MacroSeries("CIVPART", "US labor-force participation rate", "US", "labor", "BLS", 4),
    MacroSeries("AWHMAN", "US manufacturing average weekly hours", "US", "labor_manufacturing", "BLS", 4),
    MacroSeries("CES0500000003", "US average hourly earnings total private", "US", "labor_wages", "BLS", 4),
    MacroSeries("GDP", "US nominal GDP", "US", "growth", "BEA", 4),
    MacroSeries("GDPC1", "US real GDP", "US", "growth", "BEA", 5),
    MacroSeries("A191RL1Q225SBEA", "US real GDP percent change", "US", "growth", "BEA", 5),
    MacroSeries("INDPRO", "US industrial production", "US", "manufacturing_industry", "Federal Reserve", 5),
    MacroSeries("HOUST", "US housing starts", "US", "housing", "Census/FRED", 4),
    MacroSeries("PERMIT", "US building permits", "US", "housing", "Census/FRED", 4),
    MacroSeries("UMCSENT", "University of Michigan consumer sentiment", "US", "consumer_survey", "University of Michigan/FRED", 5),
    MacroSeries("MICH", "University of Michigan inflation expectations", "US", "inflation_expectations", "University of Michigan/FRED", 5),
    MacroSeries("GACDISA066MSFRBNY", "NY Fed Empire State current general business conditions", "US", "regional_manufacturing_survey", "FRB New York", 3),
    MacroSeries("GACDFSA066MSFRBPHI", "Philadelphia Fed current general activity diffusion index", "US", "regional_manufacturing_survey", "FRB Philadelphia", 3),
    MacroSeries("BACTSAMFRBDAL", "Dallas Fed current general business activity diffusion index", "US", "regional_manufacturing_survey", "FRB Dallas", 3),
    MacroSeries("LRUN64TTJPM156S", "Japan unemployment rate", "Japan", "labor", "OECD/FRED", 4),
    MacroSeries("IRLTLT01JPM156N", "Japan 10-year government bond yield", "Japan", "rates_curve", "OECD/FRED", 5),
    MacroSeries("JPNPROINDMISMEI", "Japan industrial production", "Japan", "manufacturing_industry", "OECD/FRED", 3, "FRED/OECD feed may lag"),
    MacroSeries("JPNPRINTO01GYSAM", "Japan industrial production year-over-year", "Japan", "manufacturing_industry", "OECD/FRED", 4, "Current fallback for stale Japan IP index feed"),
    MacroSeries("CPALTT01JPM659N", "Japan CPI year-over-year", "Japan", "inflation", "OECD/FRED", 3, "FRED/OECD feed is stale"),
    MacroSeries("LRUNTTTTKRM156S", "Korea unemployment rate", "Korea", "labor", "OECD/FRED", 4),
    MacroSeries("IRLTLT01KRM156N", "Korea 10-year government bond yield", "Korea", "rates_curve", "OECD/FRED", 5),
    MacroSeries("KORCPALTT01CTGYM", "Korea CPI contribution/growth", "Korea", "inflation", "OECD/FRED", 3, "FRED/OECD feed may lag"),
    MacroSeries("KORPROINDMISMEI", "Korea industrial production", "Korea", "manufacturing_industry", "OECD/FRED", 3, "FRED/OECD feed may lag"),
    MacroSeries("KORPRINTO01GYSAM", "Korea industrial production year-over-year", "Korea", "manufacturing_industry", "OECD/FRED", 4, "Current fallback for stale Korea IP index feed"),
    MacroSeries("LRHUTTTTEZM156S", "Euro area unemployment rate", "Europe", "labor", "OECD/FRED", 3, "FRED/OECD feed is stale"),
    MacroSeries("ECBDFR", "ECB deposit facility rate", "Europe", "policy_rates", "ECB/FRED", 5),
    MacroSeries("ECBMRRFR", "ECB main refinancing operations fixed rate", "Europe", "policy_rates", "ECB/FRED", 5),
    MacroSeries("IRLTLT01EZM156N", "Euro area 10-year government bond yield", "Europe", "rates_curve", "OECD/FRED", 4),
    MacroSeries("IRLTLT01DEM156N", "Germany 10-year government bond yield", "Europe", "rates_curve", "OECD/FRED", 5),
    MacroSeries("SLUEM1524ZSEMU", "Euro area youth unemployment rate", "Europe", "labor", "World Bank/FRED", 3, "Annual fallback; broad Euro unemployment monthly feed is stale"),
    MacroSeries("CPHPTT01EZM659N", "Euro area HICP year-over-year", "Europe", "inflation", "OECD/FRED", 3, "FRED/OECD feed is stale"),
    MacroSeries("CP0000EZ19M086NEST", "Euro area HICP total index", "Europe", "inflation", "Eurostat/FRED", 4, "Current fallback for stale Euro HICP YoY feed"),
    MacroSeries("EA19PRINTO01IXOBSAM", "Euro area industrial production", "Europe", "manufacturing_industry", "OECD/FRED", 3, "FRED/OECD feed may lag"),
    MacroSeries("EUROSTAT_EA20_HICP_CP00", "Euro area HICP total index direct from Eurostat", "Europe", "inflation", "Eurostat direct", 5, "Direct official Eurostat API source"),
    MacroSeries("EUROSTAT_EU27_UNRATE", "EU unemployment rate direct from Eurostat", "Europe", "labor", "Eurostat direct", 4, "Direct official Eurostat API source; EU27 used where Euro area API slice is unavailable"),
    MacroSeries("EUROSTAT_EA20_INDUSTRIAL_PRODUCTION", "Euro area industrial production direct from Eurostat", "Europe", "manufacturing_industry", "Eurostat direct", 4, "Direct official Eurostat API source"),
    MacroSeries("ESTAT_JP_CPI", "Japan CPI direct from e-Stat", "Japan", "inflation", "Japan e-Stat direct", 5, "Requires ESTAT_APP_ID and native series mapping"),
    MacroSeries("ESTAT_JP_UNRATE", "Japan unemployment rate direct from e-Stat", "Japan", "labor", "Japan e-Stat direct", 5, "Requires ESTAT_APP_ID and native series mapping"),
    MacroSeries("ESTAT_JP_INDUSTRIAL_PRODUCTION", "Japan industrial production direct from e-Stat/METI", "Japan", "manufacturing_industry", "Japan e-Stat direct", 4, "Requires ESTAT_APP_ID and native series mapping"),
    MacroSeries("IUDSOIA", "United Kingdom SONIA overnight rate", "UK", "policy_rates", "Bank of England/FRED", 5, "Current keyless BOE policy-rate proxy"),
    MacroSeries("IRLTLT01GBM156N", "United Kingdom 10-year government bond yield", "UK", "rates_curve", "OECD/FRED", 5),
)


def _dedupe(series: tuple[MacroSeries, ...]) -> tuple[MacroSeries, ...]:
    output: dict[str, MacroSeries] = {}
    for item in series:
        output.setdefault(item.series_id.upper(), item)
    return tuple(output.values())


FRED_UNIVERSES: dict[str, tuple[MacroSeries, ...]] = {
    "global_core": _dedupe(FRED_GLOBAL_CORE_SERIES),
    "us_core": _dedupe(tuple(item for item in FRED_GLOBAL_CORE_SERIES if item.region == "US")),
    "international_core": _dedupe(tuple(item for item in FRED_GLOBAL_CORE_SERIES if item.region != "US")),
}

DIRECT_SOURCE_FAMILIES = {
    "Eurostat direct",
    "Japan e-Stat direct",
    "KOSIS direct",
    "BOK ECOS direct",
}

FRED_SOURCE_DESK_CONTEXT_SERIES = (
    "FEDFUNDS",
    "SOFR",
    "DGS3MO",
    "DGS2",
    "DGS10",
    "DGS30",
    "T10Y2Y",
    "T10Y3M",
    "BAMLH0A0HYM2",
    "VIXCLS",
    "DTWEXBGS",
    "DCOILWTICO",
    "DCOILBRENTEU",
    "CPIAUCSL",
    "PCEPI",
    "PAYEMS",
    "UNRATE",
    "ICSA",
    "UMCSENT",
    "MICH",
    "IRLTLT01JPM156N",
    "LRUN64TTJPM156S",
    "IRLTLT01KRM156N",
    "LRUNTTTTKRM156S",
    "ECBDFR",
    "ECBMRRFR",
    "IRLTLT01DEM156N",
    "IUDSOIA",
    "IRLTLT01GBM156N",
)

FRED_SCORER_CONTEXT_SERIES = (
    *FRED_SOURCE_DESK_CONTEXT_SERIES,
    "EFFR",
    "MORTGAGE30US",
    "BAMLC0A0CM",
    "WALCL",
    "RRPONTSYD",
    "M2SL",
    "BUSLOANS",
    "TOTLL",
    "DEXUSEU",
    "DEXJPUS",
    "DEXCHUS",
    "DEXKOUS",
    "CPILFESL",
    "PPIACO",
    "PCEPILFE",
    "PCE",
    "RSAFS",
    "RSXFS",
    "IC4WSA",
    "CCSA",
    "JTSJOL",
    "CIVPART",
    "CES0500000003",
    "GDPC1",
    "A191RL1Q225SBEA",
    "INDPRO",
    "HOUST",
    "PERMIT",
    "GACDISA066MSFRBNY",
    "GACDFSA066MSFRBPHI",
    "BACTSAMFRBDAL",
    "IRLTLT01JPM156N",
    "JPNPRINTO01GYSAM",
    "IRLTLT01KRM156N",
    "KORCPALTT01CTGYM",
    "KORPROINDMISMEI",
    "KORPRINTO01GYSAM",
    "ECBDFR",
    "ECBMRRFR",
    "IRLTLT01EZM156N",
    "IRLTLT01DEM156N",
    "SLUEM1524ZSEMU",
    "CP0000EZ19M086NEST",
    "EUROSTAT_EA20_HICP_CP00",
    "EUROSTAT_EU27_UNRATE",
    "EUROSTAT_EA20_INDUSTRIAL_PRODUCTION",
    "ESTAT_JP_CPI",
    "ESTAT_JP_UNRATE",
    "ESTAT_JP_INDUSTRIAL_PRODUCTION",
    "IUDSOIA",
    "IRLTLT01GBM156N",
    "CPHPTT01EZM659N",
    "EA19PRINTO01IXOBSAM",
)


def series_ids_for_universe(name: str = "global_core") -> tuple[str, ...]:
    try:
        return tuple(item.series_id.upper() for item in FRED_UNIVERSES[name])
    except KeyError as exc:
        raise ValueError(f"Unknown macro universe: {name}") from exc


def metadata_by_series(name: str = "global_core") -> dict[str, MacroSeries]:
    try:
        series = FRED_UNIVERSES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown macro universe: {name}") from exc
    return {item.series_id.upper(): item for item in series}


def fred_series_ids_for_universe(name: str = "global_core") -> tuple[str, ...]:
    try:
        series = FRED_UNIVERSES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown macro universe: {name}") from exc
    return tuple(
        item.series_id.upper()
        for item in series
        if item.source_family not in DIRECT_SOURCE_FAMILIES
    )


FRED_YTD_PIPELINE_SERIES = fred_series_ids_for_universe("global_core")
