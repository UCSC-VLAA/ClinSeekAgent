# mm_bench task classification — summary

Total rows: 2695
Parsed OK: 2695   parse-failure fallback: 0

## Fine-grained task counts per coarse task

### coarse_task = `ehrxqa_image`
| task | count | % |
| --- | ---: | ---: |
| cxr_change_comparison | 222 | 44.7% |
| cxr_finding_presence | 177 | 35.6% |
| cxr_finding_enumeration | 98 | 19.7% |

### coarse_task = `ehrxqa_table`
| task | count | % |
| --- | ---: | ---: |
| ehrxqa_patient_lookup | 1078 | 63.2% |
| ehrxqa_cohort_query | 626 | 36.7% |
| prescriptions | 1 | 0.1% |
| other | 1 | 0.1% |

### coarse_task = `medmod_decompensation`
| task | count | % |
| --- | ---: | ---: |
| mortality_24h | 125 | 100.0% |

### coarse_task = `medmod_in_hospital_mortality`
| task | count | % |
| --- | ---: | ---: |
| Inpatient_Mortality | 125 | 100.0% |

### coarse_task = `medmod_phenotyping`
| task | count | % |
| --- | ---: | ---: |
| phenotype_group_assignment | 120 | 100.0% |

### coarse_task = `medmod_radiology`
| task | count | % |
| --- | ---: | ---: |
| cxr_finding_enumeration | 122 | 100.0% |

## Multimodal distribution per coarse task

| coarse_task | rows | is_mm=true | mm_kind breakdown |
| --- | ---: | ---: | --- |
| ehrxqa_image | 497 | 497 | cxr_image=425, both=72 |
| ehrxqa_table | 1706 | 0 | none=1706 |
| medmod_decompensation | 125 | 125 | cxr_image=125 |
| medmod_in_hospital_mortality | 125 | 125 | cxr_image=125 |
| medmod_phenotyping | 120 | 119 | cxr_image=119, none=1 |
| medmod_radiology | 122 | 122 | cxr_image=122 |

## Global fine-grained task distribution (across all rows)

| task | count | is_mm=true | is_mm=false |
| --- | ---: | ---: | ---: |
| ehrxqa_patient_lookup | 1078 | 0 | 1078 |
| ehrxqa_cohort_query | 626 | 0 | 626 |
| cxr_change_comparison | 222 | 222 | 0 |
| cxr_finding_enumeration | 220 | 220 | 0 |
| cxr_finding_presence | 177 | 177 | 0 |
| mortality_24h | 125 | 125 | 0 |
| Inpatient_Mortality | 125 | 125 | 0 |
| phenotype_group_assignment | 120 | 119 | 1 |
| prescriptions | 1 | 0 | 1 |
| other | 1 | 0 | 1 |
