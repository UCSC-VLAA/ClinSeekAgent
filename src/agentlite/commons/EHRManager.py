import pandas as pd
import sqlite3
import os
import glob
import warnings
import json
import datetime

from typing import Annotated
from pydantic import Field, BaseModel

from .temporal_policy import (
    TemporalPolicyError,
    build_runtime_view,
)
from .multimodal_temporal_policy import (
    MultimodalTemporalPolicyError,
    build_multimodal_runtime_view,
    detect_multimodal_schema,
)


class EHRManager:
    def __init__(self, data_path, temporal_policy_mode="auto"):
        self.data_path = data_path
        if temporal_policy_mode not in {"auto", "strict_mimic_iv", "legacy"}:
            raise ValueError(
                "temporal_policy_mode must be one of: auto, strict_mimic_iv, legacy"
            )
        self.temporal_policy_mode = temporal_policy_mode
        self.reference_db_path = os.path.join(data_path, "database", "reference_table.db")
        self.candidate_db_path = os.path.join(data_path, "database", "candidate_table.db")
        self.ehr_data = {}
        self.reference_data = {}
        self.candidate_data = {}
        self.last_temporal_report = None
        self.last_subject_id = None
        self.last_prediction_time = None
        self.temporal_policy_version = None

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
        self.last_temporal_report = None
        self.last_subject_id = None
        self.last_prediction_time = None
        self.temporal_policy_version = None

    @staticmethod
    def _quote_identifier(identifier):
        return '"' + str(identifier).replace('"', '""') + '"'

    def _read_patient_tables(self, conn):
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        table_names = [row[0] for row in cursor.fetchall()]
        return {
            table_name: pd.read_sql_query(
                f"SELECT * FROM {self._quote_identifier(table_name)}",
                conn,
            )
            for table_name in table_names
        }

    @staticmethod
    def _looks_like_mimic_iv(source_tables):
        """Distinguish the lowercase MIMIC-IV schema from legacy MIMIC-III."""

        patients = source_tables.get("patients")
        if patients is not None:
            patient_columns = {str(column).lower() for column in patients.columns}
            if "anchor_year" in patient_columns:
                return True
        return "emar" in source_tables or "edstays" in source_tables

    @staticmethod
    def _legacy_runtime_view(source_tables, timestamp):
        """Preserve the original loader for legacy MIMIC-III compatibility."""

        cutoff = pd.to_datetime(timestamp)
        legacy_tables = {}
        for table_name, full_df in source_tables.items():
            columns = list(full_df.columns)
            timestamp_col = next(
                (
                    column
                    for column in columns
                    if "time" in column.lower() or "date" in column.lower()
                ),
                None,
            )
            if timestamp_col is None:
                legacy_tables[table_name] = full_df
                continue

            sample_values = full_df[timestamp_col].dropna().head(10)
            is_date_only = all(
                not (len(str(value).strip()) > 10 and " " in str(value).strip())
                for value in sample_values
            )
            parsed = pd.to_datetime(full_df[timestamp_col], errors="coerce")
            if is_date_only:
                parsed = parsed.dt.normalize() + pd.Timedelta(
                    hours=23,
                    minutes=59,
                )
            truncated = full_df.loc[parsed < cutoff].copy()
            truncated[timestamp_col] = parsed.loc[truncated.index]
            legacy_tables[table_name] = truncated.reset_index(drop=True)
        return legacy_tables

    def load_ehr_for_sample(
        self,
        subject_id: str,
        timestamp: str,
        *,
        linked_study_ids=None,
        scope=None,
        task=None,
        hadm_id=None,
        target_hadm_id=None,
        target_stay_id=None,
        modalities=None,
    ):
        """
        Load one patient and build a prediction-time-specific runtime view.

        MIMIC-IV and the EHRXQA/MedMod multimodal schemas use strict,
        table-aware temporal policies. Other legacy MIMIC-III databases retain
        the previous first-timestamp behavior in ``auto`` mode.
        """
        db_file_path = self._get_db_file_path(subject_id)
        if not os.path.exists(db_file_path):
            raise FileNotFoundError(f"Database file not found: {db_file_path}")

        self.ehr_data = {}
        self.last_temporal_report = None
        self.last_subject_id = str(subject_id)
        self.last_prediction_time = str(timestamp)
        self.temporal_policy_version = None
        self.load_patient_table_logs = ["Loading EHR Tables:"]

        conn = None
        try:
            conn = sqlite3.connect(db_file_path)
            source_tables = self._read_patient_tables(conn)
        except sqlite3.Error as e:
            raise RuntimeError(f"Database error: {e}")
        finally:
            if conn is not None:
                conn.close()

        multimodal_schema = (
            detect_multimodal_schema(source_tables)
            if self.temporal_policy_mode == "auto"
            else None
        )
        use_strict_policy = self.temporal_policy_mode == "strict_mimic_iv" or (
            self.temporal_policy_mode == "auto"
            and self._looks_like_mimic_iv(source_tables)
        )
        if multimodal_schema is not None:
            try:
                result = build_multimodal_runtime_view(
                    source_tables,
                    timestamp,
                    linked_study_ids=linked_study_ids,
                    modalities=modalities,
                )
            except MultimodalTemporalPolicyError:
                self.ehr_data = {}
                raise
            self.ehr_data = result.tables
            self.last_temporal_report = result.report_dict()
            self.last_temporal_report["scope"] = scope
            self.last_temporal_report["task"] = task
            self.last_temporal_report["linked_study_ids"] = list(
                linked_study_ids or []
            )
            self.last_temporal_report["modalities"] = list(modalities or [])
            self.last_prediction_time = str(result.cutoff)
            self.temporal_policy_version = result.policy_version
            for table_name, report in sorted(result.reports.items()):
                self.load_patient_table_logs.append(
                    " - "
                    f"'{table_name}': input={report.input_rows}, "
                    f"loaded={report.output_rows}, "
                    f"dropped={report.dropped_rows}, "
                    f"redacted={report.redacted_values}; "
                    f"{report.reason}"
                )
        elif use_strict_policy:
            try:
                result = build_runtime_view(
                    source_tables,
                    timestamp,
                    task=task,
                    index_hadm_id=hadm_id,
                    target_hadm_id=target_hadm_id,
                    target_stay_id=target_stay_id,
                )
            except TemporalPolicyError:
                # Do not leave partially loaded patient data behind.
                self.ehr_data = {}
                raise
            self.ehr_data = result.tables
            self.last_temporal_report = result.report_dict()
            self.last_temporal_report["task"] = task
            self.last_temporal_report["index_hadm_id"] = (
                result.index_hadm_id
            )
            self.last_temporal_report["target_hadm_id"] = (
                result.target_hadm_id
            )
            self.last_temporal_report["target_stay_id"] = (
                result.target_stay_id
            )
            self.last_prediction_time = str(result.cutoff)
            self.temporal_policy_version = result.policy_version
            for table_name, report in sorted(result.reports.items()):
                self.load_patient_table_logs.append(
                    " - "
                    f"'{table_name}': input={report.input_rows}, "
                    f"loaded={report.output_rows}, "
                    f"dropped={report.dropped_rows}, "
                    f"redacted={report.redacted_values}"
                )
        else:
            self.ehr_data = self._legacy_runtime_view(source_tables, timestamp)
            self.temporal_policy_version = "legacy-first-timestamp-v1"
            self.last_temporal_report = {
                "policy_version": self.temporal_policy_version,
                "cutoff": str(pd.to_datetime(timestamp)),
                "tables": {
                    table_name: {
                        "input_rows": len(source_tables[table_name]),
                        "output_rows": len(frame),
                    }
                    for table_name, frame in sorted(self.ehr_data.items())
                },
            }
            for table_name, frame in sorted(self.ehr_data.items()):
                self.load_patient_table_logs.append(
                    f" - Loading '{table_name}' with {len(frame)} rows "
                    "(legacy policy)."
                )

        if "prescriptions" in self.ehr_data and "ndc" in self.ehr_data["prescriptions"].columns:
            prescriptions = self.ehr_data["prescriptions"].copy()
            prescriptions["ndc"] = (
                prescriptions["ndc"]
                .astype(str)
                .str.split(".", n=1)
                .str[0]
                .str.zfill(11)
            )
            self.ehr_data["prescriptions"] = prescriptions

        self.load_join_table()

        policy_log = (
            f"Temporal policy: {self.temporal_policy_version}; "
            f"subject_id={self.last_subject_id}; "
            f"cutoff={self.last_prediction_time}"
        )
        self.load_patient_table_logs.insert(0, policy_log)
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
            ehr_data_json[table_name] = {
                column: [
                    self._json_safe_value(value)
                    for value in df[column].tolist()
                ]
                for column in df.columns
            }
        return ehr_data_json

    @staticmethod
    def _json_safe_value(value):
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(value, datetime.datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, datetime.date):
            return value.isoformat()
        if hasattr(value, "item") and not isinstance(value, (str, bytes)):
            try:
                return value.item()
            except (TypeError, ValueError):
                pass
        return value
    
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
