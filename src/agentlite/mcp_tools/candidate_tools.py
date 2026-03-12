import os
import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from typing import Union, List
from thefuzz import fuzz

from typing import Annotated
from pydantic import Field
from fastmcp import Context

from agentlite.commons.fastmcp import mcp
from agentlite.mcp_tools.tool_utils import get_resource, get_resource_df

@mcp.tool(
    name="get_candidates_by_keyword",
    description="Searches for all text-based columns of the specific Candidate Table containing a specific keyword, like the code index or the item name. It is useful for finding the relative candidates without knowing the exact name of the item.",
)
async def get_candidates_by_keyword(
    ctx: Context,
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'admissions', 'notes').")],
    keyword: Annotated[str, Field(description="The keyword to search for (e.g., 'pneumonia', 'fever').")]
) -> str:
    candidate_tables = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if table_name in candidate_tables:
        df = await get_resource_df(ctx, f"cache://ehr/candidate_data/{table_name}.json")
    else:
        return f"Error: Table '{table_name}' not found in EHR table list: {candidate_tables}."
    
    if "candidate" not in df.columns:
        return f"Error: Column 'candidate' not found in table '{table_name}'."

    # 将关键词转换为小写，以进行不区分大小写的匹配
    search_keyword = keyword.lower()
    
    # 找出所有文本（object/string）类型的列
    text_cols = [col for col in df.columns if df[col].dtype in ['object', 'string']]
    
    if not text_cols:
        return f"Error: No text-based columns found in table '{table_name}' to search."
        
    # 构建一个布尔掩码，用于筛选包含关键词的行
    # 对每个文本列进行搜索，然后将结果用 OR 运算符连接
    mask = None
    for col in text_cols:
        # fillna('') 将 NaN 值替换为空字符串，以避免在 str.contains() 中出错
        col_mask = df[col].str.lower().str.contains(search_keyword, na=False)
        if mask is None:
            mask = col_mask
        else:
            mask = mask | col_mask
            
    # 应用掩码，获取匹配的记录
    matching_records = df[mask]
    
    if matching_records.empty:
        return f"No records found in table '{table_name}' containing the keyword '{keyword}'."
    
    matching_records = matching_records.drop_duplicates(subset=["candidate"], keep='first')
    
    # 返回匹配记录的格式化字符串
    return matching_records.to_string(index=False)

@mcp.tool(
    name="get_candidates_by_fuzzy_matching",
    description="Finds similar items in a Candidate Table based on fuzzy matching. It is useful for identifying potential matches for medical terms, diagnoses, medications, or procedures.",
)
async def get_candidates_by_fuzzy_matching(
    ctx: Context,
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'd_icd_diagnoses').")],
    keywords: Annotated[Union[str, List[str]], Field(description="A single keyword string or a list of keyword strings to search for.")]
) -> str:
    """
    Finds similar items in a table based on fuzzy matching.
    """
    threshold = 0
    limit = 5

    df = await get_resource_df(ctx, f"cache://ehr/candidate_data/{table_name}.json")
    table_list = await get_resource(ctx, f"cache://ehr/candidate_data/table_list.json")
    if df is None:
        return f"Error: Table '{table_name}' not found in candidate table list: {table_list}."
    
    if "candidate" not in df.columns:
        return f"Error: Column 'candidate' not found in table '{table_name}'."
        
    # 确保关键词是列表，以统一处理
    if isinstance(keywords, str):
        keywords_list = [keywords]
    else:
        keywords_list = keywords

    all_results = []
    column_name = "candidate"
    for keyword in keywords_list:
        # 计算相似度
        df['similarity_score'] = df[column_name].apply(
            lambda x: fuzz.ratio(str(x).lower(), keyword.lower())
        )
        
        # --- 关键修改在这里 ---
        # 1. 按照相似度降序排序
        sorted_df = df.sort_values('similarity_score', ascending=False)
        
        # 2. 移除指定列的重复值，并保留相似度最高的那个
        deduplicated_df = sorted_df.drop_duplicates(subset=[column_name], keep='first')
        
        # 3. 筛选、排序并限制结果数量
        candidate_results = deduplicated_df[deduplicated_df['similarity_score'] >= threshold].head(limit)
        
        if not candidate_results.empty:
            result_str = candidate_results.to_string(index=False)
            all_results.append(f"--- Results for keyword '{keyword}' (threshold: {threshold}, limit: {limit}) ---\n{result_str}\n")
        else:
            all_results.append(f"--- No similar candidates found for keyword '{keyword}' (threshold: {threshold}) ---\n")

    return "\n".join(all_results)


class EmbeddingModel:
    _default_path = "/sfs/data/ShareModels/Embeddings/BioLORD-2023"
    _local_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "models", "BioLORD-2023")

    def __init__(
        self,
        model_path: str = None,
    ):
        if model_path is None:
            model_path = self._default_path if os.path.exists(self._default_path) else self._local_path
        self.model_path = model_path
        self.embedding_cache = {}
        self.load_model()
    
    def load_model(self):
        self.model = SentenceTransformer(self.model_path)
        
    def _get_embeddings(self, texts: List[str]) -> np.ndarray:
        """Generate embeddings for a list of texts using BioLORD-2023"""
        if self.model is None:
            raise RuntimeError("BioLORD-2023 model is not loaded. Cannot generate embeddings.")
        
        # Generate embeddings
        embeddings = self.model.encode(texts, convert_to_tensor=True)
        
        # Convert to numpy array and normalize
        if isinstance(embeddings, torch.Tensor):
            embeddings = embeddings.cpu().numpy()
        
        # Normalize embeddings for cosine similarity
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)

        # import pdb; pdb.set_trace()
        normalized_embeddings = embeddings / (norms + 1e-8)  # Add small epsilon to avoid division by zero
        
        return normalized_embeddings

    def _calculate_similarity(self, query_embedding: np.ndarray, candidate_embeddings: np.ndarray) -> np.ndarray:
        """Calculate cosine similarity between query and candidate embeddings"""
        # Cosine similarity = dot product of normalized vectors
        similarities = np.dot(candidate_embeddings, query_embedding.T).flatten()
        return similarities

    def _get_cached_embeddings(self, table_name: str, candidate_texts: List[str]) -> np.ndarray:
        cache_key = f"{table_name}"

        if cache_key not in self.embedding_cache:
            self.embedding_cache[cache_key] = self._get_embeddings(candidate_texts)
        
        return self.embedding_cache[cache_key]


ss_model = EmbeddingModel()

@mcp.tool(
    name="get_candidates_by_semantic_similarity",
    description="Performs semantic search using BioLORD-2023 embeddings to find semantically similar unique entities in a specified Candidate Table. Returns distinct entities ranked by semantic similarity, avoiding duplicates. Particularly effective for medical terminology and clinical concepts.",
)
async def get_candidates_by_semantic_similarity(
    ctx: Context,
    table_name: Annotated[str, Field(description="The name of the table to search in (e.g., 'd_icd_diagnoses').")],
    query: Annotated[Union[str, List[str]], Field(description="A single keyword string or a list of keyword strings to search for.")]
) -> str:
    """
    Performs semantic search using BioLORD-2023 embeddings.
    
    Args:
        table_name (str): The name of the table to search.
        query (List[str]): Query string(s) for semantic search.
        
    Returns:
        str: Formatted results or error message.
    """
    threshold = 0.5
    limit = 5
    
    # if ctx.get_state("ss_model_loaded") is None:
    #     all_results.append("Loading Embeddding Model..." + str(ctx.get_state("ss_model_loaded")))
    #     try:
    #         ss_model.load_model()
    #         ctx.set_state("ss_model_loaded", True)
    #     except Exception as e:
    #         return f"Error: BioLORD-2023 model failed to load. Cannot perform semantic search. {str(e)}"
    
    # Get table from EHRManager
    df = await get_resource_df(ctx, f"cache://ehr/candidate_data/{table_name}.json")
    if df is None:
        return f"Error: Table '{table_name}' not found in candidate tables."

    # Ensure query is a list for uniform processing
    if isinstance(query, str):
        queries = [query]
    else:
        queries = query

    all_results = []

    try:
        # Get all unique candidate texts for embedding calculation
        candidate_texts = df["candidate"].astype(str).unique().tolist()
        candidate_embeddings = ss_model._get_cached_embeddings(table_name, candidate_texts)
        
        for query_text in queries:
            # Generate embedding for the query
            query_embedding = ss_model._get_embeddings([query_text])
            
            # Calculate similarities for unique candidates
            similarities = ss_model._calculate_similarity(query_embedding, candidate_embeddings)
            
            # Create a mapping from candidate text to similarity score
            candidate_to_similarity = dict(zip(candidate_texts, similarities))
            
            # Map similarities back to the full dataframe (preserving all IDs)
            df_copy = df.copy()
            df_copy['semantic_similarity'] = df_copy['candidate'].astype(str).map(candidate_to_similarity)
            
            # Sort by semantic similarity (keeping all ID records)
            sorted_results = df_copy.sort_values('semantic_similarity', ascending=False)
            
            # Filter by threshold first (keeping all ID records that meet threshold)
            threshold_filtered = sorted_results[
                sorted_results['semantic_similarity'] >= threshold
            ]
            
            # Now deduplicate by candidate name, keeping the highest similarity score for each name
            # This ensures we don't lose IDs but only return unique candidate names in final output
            deduplicated_results = threshold_filtered.drop_duplicates(subset=["candidate"], keep='first')
            
            # Apply limit to final results
            filtered_results = deduplicated_results.head(limit)
            
            if not filtered_results.empty:
                # Format similarity scores to 3 decimal places
                filtered_results['semantic_similarity'] = filtered_results['semantic_similarity'].round(3)
                
                unique_entities = len(filtered_results)
                result_str = filtered_results.to_string(index=False)
                all_results.append(f"--- Semantic search results for '{query_text}' ---")
                all_results.append(f"Found {unique_entities} unique candidate names (threshold: {threshold}, limit: {limit})")
                all_results.append(f"{result_str}\n")
            else:
                all_results.append(f"--- No semantically similar candidates found for '{query_text}' (threshold: {threshold}) ---\n")
        
        return "\n".join(all_results)
        
    except Exception as e:
        return f"An error occurred during semantic search: {str(e)}"
    

def local_test(query='a'):
        
    threshold = 0.5
    limit = 5
    
    # if ctx.get_state("ss_model_loaded") is None:
    #     all_results.append("Loading Embeddding Model..." + str(ctx.get_state("ss_model_loaded")))
    #     try:
    #         ss_model.load_model()
    #         ctx.set_state("ss_model_loaded", True)
    #     except Exception as e:
    #         return f"Error: BioLORD-2023 model failed to load. Cannot perform semantic search. {str(e)}"

    # Ensure query is a list for uniform processing
    if isinstance(query, str):
        queries = [query]
    else:
        queries = query

    all_results = []

# Get all unique candidate texts for embedding calculation
    candidate_texts = ["a", "b", "c", "d", "e", "f", "g"]
    candidate_embeddings = ss_model._get_cached_embeddings("test", candidate_texts)
    
    for query_text in queries:
        # Generate embedding for the query
        query_embedding = ss_model._get_embeddings([query_text])
        
        # Calculate similarities for unique candidates
        similarities = ss_model._calculate_similarity(query_embedding, candidate_embeddings)
        
        # Create a mapping from candidate text to similarity score
        candidate_to_similarity = dict(zip(candidate_texts, similarities))
        
        # Map similarities back to the full dataframe (preserving all IDs)
        df_copy = df.copy()
        df_copy['semantic_similarity'] = df_copy['candidate'].astype(str).map(candidate_to_similarity)
        
        # Sort by semantic similarity (keeping all ID records)
        sorted_results = df_copy.sort_values('semantic_similarity', ascending=False)
        
        # Filter by threshold first (keeping all ID records that meet threshold)
        threshold_filtered = sorted_results[
            sorted_results['semantic_similarity'] >= threshold
        ]
        
        # Now deduplicate by candidate name, keeping the highest similarity score for each name
        # This ensures we don't lose IDs but only return unique candidate names in final output
        deduplicated_results = threshold_filtered.drop_duplicates(subset=["candidate"], keep='first')
        
        # Apply limit to final results
        filtered_results = deduplicated_results.head(limit)
        
        if not filtered_results.empty:
            # Format similarity scores to 3 decimal places
            filtered_results['semantic_similarity'] = filtered_results['semantic_similarity'].round(3)
            
            unique_entities = len(filtered_results)
            result_str = filtered_results.to_string(index=False)
            all_results.append(f"--- Semantic search results for '{query_text}' ---")
            all_results.append(f"Found {unique_entities} unique candidate names (threshold: {threshold}, limit: {limit})")
            all_results.append(f"{result_str}\n")
        else:
            all_results.append(f"--- No semantically similar candidates found for '{query_text}' (threshold: {threshold}) ---\n")
    
    return "\n".join(all_results)

if __name__ == '__main__':
    local_test()

# @register("semantic_search")
# class SemanticSearchAction(EHRAction):
#     def __init__(self, ehr_manager: EHRManager):
#         super().__init__(
#             action_name="SemanticSearch",
#             action_desc="Performs semantic search using BioLORD-2023 embeddings to find semantically similar unique entities in a specified candidate table. Returns distinct entities ranked by semantic similarity, avoiding duplicates. Particularly effective for medical terminology and clinical concepts.",
#             params_doc={
#                 "table_name": "The name of the candidate table to search in (e.g., 'd_icd_diagnoses').",
#                 # "column_name": "The name of the column to perform the semantic search on.",
#                 "query": "A list of query strings to search for semantically similar content.",
#                 # "threshold": "Optional. The minimum similarity score (0.0-1.0) for an item to be considered a match. Default is 0.5.",
#                 # "limit": "Optional. The maximum number of unique entities to return for each query. Default is 3."
#             },
#             ehr_manager=ehr_manager
#         )
#         # Initialize the BioLORD-2023 model
#         self.model = None
#         self._load_model()

#     def _load_model(self):
#         """Load the BioLORD-2023 model for embedding generation"""
#         try:
#             self.model = SentenceTransformer('/remote-home/chuanxuan/model/BioLORD-2023')
#         except Exception as e:
#             self.model = None

#     def _get_embeddings(self, texts: List[str]) -> np.ndarray:
#         """Generate embeddings for a list of texts using BioLORD-2023"""
#         if self.model is None:
#             raise RuntimeError("BioLORD-2023 model is not loaded. Cannot generate embeddings.")
        
#         # Generate embeddings
#         embeddings = self.model.encode(texts, convert_to_tensor=True)
        
#         # Convert to numpy array and normalize
#         if isinstance(embeddings, torch.Tensor):
#             embeddings = embeddings.cpu().numpy()
        
#         # Normalize embeddings for cosine similarity
#         norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
#         normalized_embeddings = embeddings / (norms + 1e-8)  # Add small epsilon to avoid division by zero
        
#         return normalized_embeddings

#     def _calculate_similarity(self, query_embedding: np.ndarray, candidate_embeddings: np.ndarray) -> np.ndarray:
#         """Calculate cosine similarity between query and candidate embeddings"""
#         # Cosine similarity = dot product of normalized vectors
#         similarities = np.dot(candidate_embeddings, query_embedding.T).flatten()
#         return similarities

#     def _get_cached_embeddings(self, table_name: str, candidate_texts: List[str]) -> np.ndarray:
#         cache_key = f"{table_name}"
        
#         if not hasattr(self.ehr_manager, 'embedding_cache'):
#             self.ehr_manager.embedding_cache = {}

#         if cache_key not in self.ehr_manager.embedding_cache:
#             self.ehr_manager.embedding_cache[cache_key] = self._get_embeddings(candidate_texts)
        
#         return self.ehr_manager.embedding_cache[cache_key]

#     def _execute(self, table_name: str, query: List[str]) -> str:
#         """
#         Performs semantic search using BioLORD-2023 embeddings.
        
#         Args:
#             table_name (str): The name of the table to search.
#             query (List[str]): Query string(s) for semantic search.
            
#         Returns:
#             str: Formatted results or error message.
#         """
#         threshold = 0.5
#         limit = 5
#         if self.model is None:
#             return "Error: BioLORD-2023 model failed to load. Cannot perform semantic search."
        
#         # Get table from EHRManager
#         df = self.ehr_manager.get_candidate_table(table_name)
#         if df is None:
#             return f"Error: Table '{table_name}' not found in candidate tables."
            
        
#         # Ensure query is a list for uniform processing
#         if isinstance(query, str):
#             queries = [query]
#         else:
#             queries = query

#         try:
#             candidate_texts = df["candidate"].astype(str).tolist()
#             candidate_embeddings = self._get_cached_embeddings(table_name, candidate_texts)
            
#             all_results = []
            
#             for query_text in queries:
#                 # Generate embedding for the query
#                 query_embedding = self._get_embeddings([query_text])
                
#                 # Calculate similarities
#                 similarities = self._calculate_similarity(query_embedding, candidate_embeddings)
                
#                 # Add similarities to the valid dataframe (now lengths match)
#                 df_copy = df.copy()
#                 df_copy['semantic_similarity'] = similarities
                
#                 sorted_results = df_copy.sort_values('semantic_similarity', ascending=False)
#                 deduplicated_results = sorted_results.drop_duplicates(subset=["candidate"], keep='first')
#                 filtered_results = deduplicated_results[
#                     deduplicated_results['semantic_similarity'] >= threshold
#                 ].head(limit)
                
#                 if not filtered_results.empty:
#                     # Format similarity scores to 3 decimal places
#                     filtered_results['semantic_similarity'] = filtered_results['semantic_similarity'].round(3)
                    
#                     unique_entities = len(filtered_results)
#                     result_str = filtered_results.to_string(index=False)
#                     all_results.append(f"--- Semantic search results for '{query_text}' ---")
#                     all_results.append(f"Found {unique_entities} unique entities (threshold: {threshold}, limit: {limit})")
#                     all_results.append(f"{result_str}\n")
#                 else:
#                     all_results.append(f"--- No semantically similar candidates found for '{query_text}' (threshold: {threshold}) ---\n")
            
#             return "\n".join(all_results)
            
#         except Exception as e:
#             return f"An error occurred during semantic search: {str(e)}"