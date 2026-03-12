import json
import os
import numpy as np
from typing import List, Optional, Dict, Any, Callable
try:
    import torch
    from transformers import AutoModel, AutoTokenizer
except ImportError:
    torch = None
    AutoModel = None
    AutoTokenizer = None

from agentlite.commons.embedding_paths import resolve_bge_m3_path
from .models import MemoryEntry, MemoryItem


class EHRRetriever:
    """
    Embedding-based retrieval for EHR (Electronic Health Records) data.

    Uses cosine similarity between query embeddings and EHR record embeddings
    to find relevant patient records for a given subject_id.

    - Caching embeddings per subject_id and timestamp
    - Filtering by success status and similarity threshold
    """

    def __init__(
        self, 
        llm,  
        top_k: int = 5, 
        similarity_threshold: float = 0.5,
        cache_path: str = "/sfs/rhome/xuanchuan/EHRAgent/cache/embedding_cache.json",
        embedding_model_path: str = None,
        embedding_device: int = 0
    ):
        """
        Initialize EHR Retriever.
        
        Args:
            llm: Language model with embedding capability
            top_k: Maximum number of records to retrieve
            similarity_threshold: Minimum cosine similarity threshold (default: 0.5)
            cache_path: Path to embedding cache file
            embedding_model_path: Path to embedding model
            embedding_device: Device to use for embedding model
        """
        embedding_model_path = resolve_bge_m3_path(embedding_model_path)
        self.llm = llm
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.cache_path = cache_path
        self.embedding_cache: Dict[str, Dict[str, List[float]]] = {}  # {subject_id: {timestamp: embedding}}
        
        self.embedding_model = None
        self.embedding_tokenizer = None
        
        if embedding_model_path:
            if torch is None:
                print("Warning: torch or transformers not installed. Cannot use local embedding model.")
            else:
                self.device = f"cuda:{embedding_device}" if torch.cuda.is_available() else "cpu"
                print(f"Loading embedding model from {embedding_model_path} to {self.device}...")
                try:
                    self.embedding_tokenizer = AutoTokenizer.from_pretrained(embedding_model_path)
                    self.embedding_model = AutoModel.from_pretrained(embedding_model_path).to(self.device)
                    self.embedding_model.eval()
                    print("Embedding model loaded successfully.")
                except Exception as e:
                    print(f"Error loading embedding model: {e}")
            
        self._load_cache()

    def retrieve(
        self,
        ehr_data: Dict[str, Any],
        ehr_records: List[MemoryEntry],
        k: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
        success_only: bool = False,
        min_steps: Optional[int] = None,
        max_steps: Optional[int] = None
    ) -> List[tuple[float, MemoryEntry]]:
        """
        Retrieve similar EHR records based on embedding similarity.
        
        Args:
            ehr_data: Current EHR data dict with keys: subject_id, timestamp, table_list
            ehr_records: List of historical MemoryEntry records to search from
            k: Maximum number of records to retrieve (default: self.top_k)
            similarity_threshold: Minimum similarity threshold (default: self.similarity_threshold)
            success_only: Only retrieve successful cases (default: True)
            min_steps: Minimum number of steps in trajectory (optional)
            max_steps: Maximum number of steps in trajectory (optional)
            
        Returns:
            List[tuple[float, MemoryEntry]]: List of (similarity_score, record) tuples,
                                             sorted by similarity (descending)
        """
        if k is None:
            k = self.top_k
        if similarity_threshold is None:
            similarity_threshold = self.similarity_threshold

        subject_id = ehr_data["subject_id"]
        timestamp = ehr_data["timestamp"]
        table_list = ehr_data["table_list"]

        # Apply filters first
        filtered_records = self._filter_records(
            ehr_records,
            success_only=success_only,
            min_steps=min_steps,
            max_steps=max_steps
        )

        # Generate query embedding and cache it
        ehr_embedding = self._get_or_create_embedding_for_query(subject_id, timestamp, table_list)

        if not filtered_records:
            return []

        # Compute similarities
        similarities = []
        for record in filtered_records:
            record_embedding = self._get_or_create_ehr_embedding(record)
            sim = self._cosine_similarity(ehr_embedding, record_embedding)
            print(f"similarity: {sim}")
            
            # Apply similarity threshold filter
            if sim >= similarity_threshold:
                similarities.append((sim, record))

        # Sort by similarity (descending) and return top-k
        similarities.sort(reverse=True, key=lambda x: x[0])
        return similarities[:k]

    def _filter_records(
        self,
        records: List[MemoryEntry],
        success_only: bool = False,
        min_steps: Optional[int] = None,
        max_steps: Optional[int] = None,
        has_memory_items: bool = True
    ) -> List[MemoryEntry]:
        """
        Filter memory records based on various criteria.
        
        Args:
            records: List of MemoryEntry to filter
            success_only: Only include successful trajectories (default: True)
            min_steps: Minimum number of steps required (optional)
            max_steps: Maximum number of steps allowed (optional)
            has_memory_items: Only include entries with extracted memory items (default: True)
            
        Returns:
            List[MemoryEntry]: Filtered list of records
        """
        filtered = []
        
        for record in records:
            # Filter by success status
            if success_only and not record.success:
                continue
            
            # Filter by number of steps
            if min_steps is not None and record.steps_taken is not None:
                if record.steps_taken < min_steps:
                    continue
            
            if max_steps is not None and record.steps_taken is not None:
                if record.steps_taken > max_steps:
                    continue
            
            # Filter by memory items existence
            # if has_memory_items and (not record.memory_items or len(record.memory_items) == 0):
            #     continue
            
            filtered.append(record)
        
        return filtered

    def _get_or_create_embedding_for_query(
        self,
        subject_id: int,
        timestamp: str,
        table_list: List[Dict[str, Any]]
    ) -> List[float]:
        """
        Get or create embedding for the current query EHR data.
        This method caches the query embedding for future use.

        Args:
            subject_id: Patient subject ID
            timestamp: Timestamp of the query
            table_list: EHR table data for embedding

        Returns:
            List[float]: Embedding vector
        """
        embedding = self.embed_ehr(table_list)

        # Cache the embedding
        if str(subject_id) not in self.embedding_cache:
            self.embedding_cache[str(subject_id)] = {}
        self.embedding_cache[str(subject_id)][str(timestamp)] = embedding
        self._save_cache()

        return embedding

    def _get_or_create_ehr_embedding(
        self, 
        record: MemoryEntry
    ) -> List[float]:
        """
        Get cached embedding for an EHR record from memory_items.
        Since all records in memory_items should have cached embeddings,
        this method only reads from cache.

        Args:
            record: MemoryEntry with EHR data

        Returns:
            List[float]: Embedding vector

        Raises:
            ValueError: If embedding is not found in cache
        """
        subject_id = record.subject_id
        timestamp = record.timestamp

        # Check cache (organized by subject_id -> timestamp)
        if str(subject_id) in self.embedding_cache:
            if str(timestamp) in self.embedding_cache[str(subject_id)]:
                    return self.embedding_cache[str(subject_id)][str(timestamp)]

        # If not in cache, raise error since memory_items should always have cached embeddings
        raise ValueError(
            f"Embedding not found in cache for record: subject_id={subject_id}, "
            f"timestamp={timestamp}. "
            f"All memory_items should have pre-cached embeddings."
        )

    def embed_ehr(self, table_list: List[Dict[str, Any]]) -> List[float]:
        """
        Generate embedding for EHR data (table_list).
        
        Args:
            table_list: List of dictionaries containing EHR table data
            
        Returns:
            List[float]: Embedding vector
        """
        if not table_list:
            # Return zero vector for empty table_list
            # Assuming embedding dimension, adjust as needed
            return [0.0] * 768  # Default dimension, adjust based on your model
        
        try:
            # Convert table_list to string directly to avoid empty text from apply_chat_template
            text = str(table_list)
            
            # Check if separate local embedding model is loaded
            if self.embedding_model is not None and self.embedding_tokenizer is not None:
                # Tokenize and get embedding from local model
                inputs = self.embedding_tokenizer(
                    text, 
                    padding=True, 
                    truncation=True, 
                    return_tensors="pt", 
                    max_length=8192
                ).to(self.device)
                
                with torch.no_grad():
                    outputs = self.embedding_model(**inputs)
                    # Use CLS token embedding (first token)
                    embedding = outputs.last_hidden_state[:, 0]
                    # Normalize
                    embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)
                    return embedding[0].cpu().tolist()
            
            # If no local model loaded, return zero vector (or raise error if strictly required)
            print("Warning: No local embedding model loaded. Returning zero vector.")
            return [0.0] * 768

        except Exception as e:
            # Handle embedding errors gracefully
            print(f"Warning: Failed to generate embedding: {e}")
            return [0.0] * 768  # Return zero vector on error

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """
        Compute cosine similarity between two vectors.

        Args:
            vec1: First vector
            vec2: Second vector

        Returns:
            float: Cosine similarity in range [-1, 1]
        """
        # Convert to numpy arrays
        v1 = np.array(vec1)
        v2 = np.array(vec2)

        # Compute cosine similarity
        dot_product = np.dot(v1, v2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(dot_product / (norm1 * norm2))

    def _load_cache(self) -> None:
        """Load embedding cache from disk (organized by subject_id)."""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    self.embedding_cache = json.load(f)
            except Exception:
                # If cache is corrupted, start fresh
                self.embedding_cache = {}

    def _save_cache(self) -> None:
        """Save embedding cache to disk (organized by subject_id -> timestamp)."""
        try:
            # Create directory if it doesn't exist
            cache_dir = os.path.dirname(self.cache_path)
            if cache_dir:  # Only create if there's a directory component
                os.makedirs(cache_dir, exist_ok=True)

            with open(self.cache_path, 'w') as f:
                json.dump(self.embedding_cache, f, indent=2)
            print(f"Embedding cache saved to {self.cache_path}")
        except Exception as e:
            # Non-critical error, continue without caching
            print(f"Warning: Failed to save embedding cache: {e}")
