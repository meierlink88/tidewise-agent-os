"""Controlled values mirrored from Tidewise Data contracts for graph extraction."""

from enum import StrEnum


class ReviewStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"


class RecordStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class RegionType(StrEnum):
    CONTINENT = "CONTINENT"
    GEOGRAPHIC = "GEOGRAPHIC"
    MULTILATERAL = "MULTILATERAL"
    INVESTMENT = "INVESTMENT"


class ConceptType(StrEnum):
    TECHNOLOGY = "technology"
    POLICY = "policy"
    APPLICATION = "application"
    DEMAND = "demand"
    BUSINESS_MODEL = "business_model"
    COMPANY_ECOSYSTEM = "company_ecosystem"
    PRODUCT_ECOSYSTEM = "product_ecosystem"
    EVENT_NARRATIVE = "event_narrative"
    MARKET_THEME = "market_theme"


class BindingPowerLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class InfluenceRating(StrEnum):
    S = "S"
    A = "A"
    B = "B"


class MembershipType(StrEnum):
    FULL_MEMBER = "FULL_MEMBER"
    OBSERVER = "OBSERVER"
    ASSOCIATE = "ASSOCIATE"
    PARTNER = "PARTNER"
    CANDIDATE = "CANDIDATE"


class ContextualStage(StrEnum):
    UPSTREAM = "upstream"
    MIDSTREAM = "midstream"
    DOWNSTREAM = "downstream"


class SegmentKind(StrEnum):
    DIRECT_CANDIDATE = "direct_candidate"
    COMPRESSED_CANDIDATE = "compressed_candidate"


class AnalysisAnchorType(StrEnum):
    COUNTRY = "Country"
    REGION = "Region"
    GEOPOLITIC_RIVALRY = "GeopoliticRivalry"
    MACRO_ECONOMIC = "MacroEconomic"
    INDUSTRY_CHAIN = "IndustryChain"
    CHAIN_NODE = "ChainNode"
    CONCEPT = "Concept"
    COMPANY = "Company"
    COMMODITY_INDEX = "CommodityIndex"
    MARKET_INDEX = "MarketIndex"
    SECURITY = "Security"


class VariableRole(StrEnum):
    FUNDAMENTAL = "FUNDAMENTAL"
    INVESTMENT_ASSESSMENT = "INVESTMENT_ASSESSMENT"


class VariableGroup(StrEnum):
    DEMAND = "DEMAND"
    SUPPLY_CAPACITY = "SUPPLY_CAPACITY"
    PRICE_PROFITABILITY = "PRICE_PROFITABILITY"
    CAPITAL_CYCLE = "CAPITAL_CYCLE"
    TECHNOLOGY = "TECHNOLOGY"
    COMPETITION_SECURITY = "COMPETITION_SECURITY"
    MACRO_POLICY = "MACRO_POLICY"
    GEOPOLITICAL = "GEOPOLITICAL"
    COMPANY_FINANCIAL = "COMPANY_FINANCIAL"
    INVESTMENT_ASSESSMENT = "INVESTMENT_ASSESSMENT"


class GeopoliticRivalryType(StrEnum):
    GEOPOLITICAL = "GEOPOLITICAL"
    MILITARY_WAR = "MILITARY_WAR"


class GeopoliticRivalryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    RESOLVED = "RESOLVED"


class MacroEconomicCategory(StrEnum):
    MONETARY = "MONETARY"
    FISCAL = "FISCAL"
    INDUSTRIAL_POLICY = "INDUSTRIAL_POLICY"
    GROWTH_CYCLE = "GROWTH_CYCLE"
    INFLATION_PRICES = "INFLATION_PRICES"
    EMPLOYMENT_LABOR = "EMPLOYMENT_LABOR"
    FINANCIAL_STABILITY = "FINANCIAL_STABILITY"
    EXTERNAL_SECTOR = "EXTERNAL_SECTOR"
    DEBT_LEVERAGE = "DEBT_LEVERAGE"
    REAL_ESTATE = "REAL_ESTATE"


class MacroEconomicStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    ARCHIVED = "ARCHIVED"
