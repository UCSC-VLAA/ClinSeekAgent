import pandas as pd
import sqlite3
import os
import glob
import warnings
import json

from typing import Annotated
from pydantic import Field, BaseModel

class EHRManager:
    def __init__(self, data_path):
        self.data_path = data_path
        self.reference_db_path = os.path.join(data_path, "database", "reference_table.db")
        self.candidate_db_path = os.path.join(data_path, "database", "candidate_table.db")
        self.ehr_data = {}
        self.reference_data = {}
        self.candidate_data = {}

        self.load_reference_table_log = None
        self._load_reference_data()
        self.load_candidate_table_log = None
        self._load_candidate_data()

        if 'd_atc_prescriptions' in self.reference_data:
            self.reference_data["d_atc_prescriptions"]["ndc"] = self.reference_data["d_atc_prescriptions"]["ndc"].astype(str).str.split(".", n=1).str[0].str.zfill(11)
        if "prescriptions_atc_candidates" in self.candidate_data:
            self.candidate_data["prescriptions_atc_candidates"]["ndc"] = self.candidate_data["prescriptions_atc_candidates"]["ndc"].astype(str).str.split(".", n=1).str[0].str.zfill(11)

        self.link_info_path = os.path.join(data_path, "table_description", "link_information.json")
        self.descriptions_path = os.path.join(data_path, "table_description", "shorten_description.json")
        self.link_info = []
        self._load_link_info()
        self.descriptions = []
        self._load_descriptions()

    def _load_link_info(self):
        if not os.path.exists(self.link_info_path):
            warnings.warn(f"Link information not found at {self.link_info_path}. Link information will be unavailable.")
            return
        
        with open(self.link_info_path, 'r') as f:
            self.link_info = [json.loads(line) for line in f.readlines()]

    def _load_descriptions(self):
        if not os.path.exists(self.descriptions_path):
            warnings.warn(f"Descriptions not found at {self.descriptions_path}. Descriptions will be unavailable.")
            return

        with open(self.descriptions_path, 'r') as f:
            self.descriptions = [json.loads(line) for line in f.readlines()]
        
        file_name_to_index = {table_info.get("file_name"): idx for idx, table_info in enumerate(self.descriptions)}
        for link_info in self.link_info:
            table_name = link_info['table_name']
            link_table = link_info['link_table']
            idx = file_name_to_index.get(table_name)
            link_idx = file_name_to_index.get(link_table)
            if idx is not None and link_idx is not None:
                columns = self.descriptions[idx].get("columns", [])
                link_columns = self.descriptions[link_idx].get("columns", [])
                
                existing_column_names = {col["column_name"] for col in columns}
                merged_columns = columns.copy()
                
                for link_col in link_columns:
                    if link_col["column_name"] not in existing_column_names:
                        merged_columns.append(link_col)
                
                self.descriptions[idx]["columns"] = merged_columns

    def _load_reference_data(self):
        """加载参考数据库中的所有表格到内存。"""
        if not os.path.exists(self.reference_db_path):
            warnings.warn(f"Reference database not found at {self.reference_db_path}. Mappings will be unavailable.")
            return

        try:
            conn = sqlite3.connect(self.reference_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            table_names = [row[0] for row in cursor.fetchall()]
            
            for table_name in table_names:
                query = f"SELECT * FROM {table_name}"
                self.reference_data[table_name] = pd.read_sql_query(query, conn)
            
            self.load_reference_table_log = f"Loading Reference Tables: {list(self.reference_data.keys())}"
        except sqlite3.Error as e:
            raise RuntimeError(f"Error loading reference database: {e}")
        finally:
            if conn:
                conn.close()
    
    def _load_candidate_data(self):
        if not os.path.exists(self.candidate_db_path):
            warnings.warn(f"Candidate database not found at {self.candidate_db_path}. Candidate data will be unavailable.")
            return

        try:
            conn = sqlite3.connect(self.candidate_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            table_names = [row[0] for row in cursor.fetchall()]
            
            for table_name in table_names:
                query = f"SELECT * FROM {table_name}"
                self.candidate_data[table_name] = pd.read_sql_query(query, conn)
            
            load_candidate_table_log = ["Loading Candidate Tables:"]
            for table_name in self.candidate_data.keys():
                load_candidate_table_log.append(f" - Loading '{table_name}' with {len(self.candidate_data[table_name])} rows.")
            self.load_candidate_table_log = "\n".join(load_candidate_table_log)

        except sqlite3.Error as e:
            raise RuntimeError(f"Error loading candidate database: {e}")
        finally:
            if conn:
                conn.close()

    def _get_db_file_path(self, subject_id):
        # 你的逻辑，例如 'sample_1.db'
        return os.path.join(self.data_path, "database", f"patient_{subject_id}.db")
    
    def unload_ehr(self):
        self.ehr_data = {}

    def load_ehr_for_sample(self, subject_id: str, timestamp: str):
        """
        Use the action to load the ehr data for the given subject_id and timestamp. This action should be taken once at the beginning of each task.
        """
        db_file_path = self._get_db_file_path(subject_id)
        if not os.path.exists(db_file_path):
            raise FileNotFoundError(f"Database file not found: {db_file_path}")
        
        self.ehr_data = {}

        self.load_patient_table_logs = ["Loading EHR Tables:"]
        
        try:
            conn = sqlite3.connect(db_file_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            table_names = [row[0] for row in cursor.fetchall()]
            
            for table_name in table_names:
                # 尝试找到时间戳列名
                query = f"PRAGMA table_info({table_name});"
                cursor.execute(query)
                columns = [col[1] for col in cursor.fetchall()]
                timestamp_col = next((col for col in columns if 'time' in col.lower() or 'date' in col.lower()), None)
                
                # 读取数据并进行时间戳截断
                full_df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
                
                if timestamp_col and timestamp_col in full_df.columns:
                    sample_values = full_df[timestamp_col].dropna().head(10)
                    is_date_only = True
                    for value in sample_values:
                        if pd.isna(value):
                            continue
                        value_str = str(value).strip()
                        if len(value_str) > 10 and ' ' in value_str:
                            is_date_only = False
                            break
                    
                    full_df[timestamp_col] = pd.to_datetime(full_df[timestamp_col], errors='coerce')
                    
                    if is_date_only:
                        full_df[timestamp_col] = full_df[timestamp_col].dt.normalize() + pd.Timedelta(hours=23, minutes=59)
                    full_df[timestamp_col] = pd.to_datetime(full_df[timestamp_col], errors='coerce')
                    truncated_df = full_df[full_df[timestamp_col] < pd.to_datetime(timestamp)]
                    self.ehr_data[table_name] = truncated_df
                    self.load_patient_table_logs.append(f" - Loading '{table_name}' with {len(truncated_df)} rows.")
                else:
                    self.ehr_data[table_name] = full_df
                    self.load_patient_table_logs.append(f" - Loading '{table_name}' with {len(full_df)} rows.")
        except sqlite3.Error as e:
            raise RuntimeError(f"Database error: {e}")
        finally:
            if conn:
                conn.close()

        # if self.task == "prescriptions":
        if "pharmacy" in self.ehr_data:
            self.ehr_data["pharmacy"] = self.ehr_data["pharmacy"].iloc[0:0]
        if "prescriptions" in self.ehr_data and "ndc" in self.ehr_data["prescriptions"].columns:
            self.ehr_data["prescriptions"]["ndc"] = self.ehr_data["prescriptions"]["ndc"].astype(str).str.split(".", n=1).str[0].str.zfill(11)

        self.load_join_table()
        
        # if self.load_reference_table_log is not None and self.load_candidate_table_log is not None:
        #     load_logs = "\n".join([self.load_reference_table_log, self.load_candidate_table_log] + self.load_patient_table_logs)
        # elif self.load_reference_table_log is not None:
        #     load_logs = "\n".join([self.load_reference_table_log] + self.load_patient_table_logs)
        if self.load_candidate_table_log is not None:
            load_logs = "\n".join([self.load_candidate_table_log] + self.load_patient_table_logs)
        else:
            load_logs = "\n".join(self.load_patient_table_logs)

        return load_logs
    
    def load_join_table(self):
        for link_info in self.link_info:
            table_name = link_info['table_name']
            link_table = link_info['link_table']
            link_column = link_info['link_column']
            
            if table_name in self.ehr_data and link_table in self.reference_data:
                self.ehr_data[table_name] = pd.merge(self.ehr_data[table_name], self.reference_data[link_table], on=link_column, how='left')

    def get_table(self, table_name):
        if table_name in self.ehr_data:
            return self.ehr_data.get(table_name)
        
        elif table_name in self.reference_data:
            return self.reference_data[table_name]

        else:
            # raise KeyError(f"Table {table_name} not in Patient Record Table ({self.ehr_data.keys()}) and not in Reference Table ({self.reference_data.keys()})")
            return None
    
    def get_candidate_table(self, table_name):
        if table_name in self.candidate_data:
            return self.candidate_data[table_name]
        else:
            return None
    
    def get_ehr_data_json(self):
        ehr_data_json = {}
        for table_name, df in self.ehr_data.items():
            df = df.astype(str)
            ehr_data_json[table_name] = df.to_dict(orient='list')
        return ehr_data_json
    
    def get_candidate_data_json(self):
        candidate_data_json = {}
        for table_name, df in self.candidate_data.items():
            df = df.astype(str)
            candidate_data_json[table_name] = df.to_dict(orient='list')
        return candidate_data_json
    
    def get_reference_data_json(self):
        reference_data_json = {}
        for table_name, df in self.reference_data.items():
            df = df.astype(str)
            reference_data_json[table_name] = df.to_dict(orient='list')
        return reference_data_json

    def get_ehr_table_names(self):
        return list(self.ehr_data.keys())
    
    def get_reference_table_names(self):
        return list(self.reference_data.keys())
    
    def get_candidate_table_names(self):
        return list(self.candidate_data.keys())
