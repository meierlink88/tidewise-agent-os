"""First-batch Graphiti entity schemas and their outbound relationships."""

from sematica.ontology.entities.chain_node import (
    ChainNode,
    ChainNodeBelongsToIndustryChain,
    ChainNodeDependsOn,
    ChainNodeInputTo,
    ChainNodeIsComponentOf,
)
from sematica.ontology.entities.company import (
    Company,
    CompanyBelongsToIndustry,
    CompanyOperatesInIndustry,
    CompanyParticipatesInChainNode,
)
from sematica.ontology.entities.concept import Concept
from sematica.ontology.entities.country import (
    Country,
    CountryImplementsMacroEconomic,
    CountryInRegion,
    CountryMemberOfOrganization,
)
from sematica.ontology.entities.geopolitic_rivalry import GeopoliticRivalry
from sematica.ontology.entities.industry import Industry, IndustryHasParent
from sematica.ontology.entities.industry_chain import (
    IndustryChain,
    IndustryChainMappedToConcept,
    IndustryChainMappedToIndustry,
)
from sematica.ontology.entities.macro_economic import MacroEconomic
from sematica.ontology.entities.organization import Organization, OrganizationInRegion
from sematica.ontology.entities.region import Region
from sematica.ontology.entities.variable import Variable

__all__ = [
    "ChainNode",
    "ChainNodeBelongsToIndustryChain",
    "ChainNodeDependsOn",
    "ChainNodeInputTo",
    "ChainNodeIsComponentOf",
    "Company",
    "CompanyBelongsToIndustry",
    "CompanyOperatesInIndustry",
    "CompanyParticipatesInChainNode",
    "Concept",
    "Country",
    "CountryInRegion",
    "CountryImplementsMacroEconomic",
    "CountryMemberOfOrganization",
    "GeopoliticRivalry",
    "Industry",
    "IndustryHasParent",
    "IndustryChain",
    "IndustryChainMappedToConcept",
    "IndustryChainMappedToIndustry",
    "MacroEconomic",
    "Organization",
    "OrganizationInRegion",
    "Region",
    "Variable",
]
