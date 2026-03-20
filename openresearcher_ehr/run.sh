export SERPER_API_KEY=61877f4a59d2968ae439a7d13d49dc2990bc0a1b

python deploy_agent.py \
    --data_path /home/efs/zlt/deepresearch/data/EHRAgentBench/common/diagnoses_ccs_500.json \
    --output_dir ./diagnoses_ccs_500_results \
    --enable_ehr \
    --max_concurrency 1 \
    --verbose
