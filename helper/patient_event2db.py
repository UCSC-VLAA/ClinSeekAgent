import os 
import pandas as pd
import json
import datetime
import jsonlines
import numpy
import tqdm
import argparse
import sqlite3
from datetime import timedelta

class MyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            print("MyEncoder-datetime.datetime")
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(obj, bytes):
            return str(obj, encoding='utf-8')
        if isinstance(obj, int):
            return str(obj)
        if isinstance(obj, float):
            return str(obj)
        elif isinstance(obj, numpy.int64):
           return str(obj)
        else:
            return super(MyEncoder, self).default(obj)


def preprocess_subject_dict(subject_dict):
    """
    在保存到数据库之前预处理subject_dict
    """
    subjects_to_remove = []
    
    for subject_id in list(subject_dict.keys()):
        try:
            # 需求（2）：为diagnoses_icd增加charttime列
            if 'diagnoses_icd' in subject_dict[subject_id] and 'admissions' in subject_dict[subject_id]:
                admissions_data = subject_dict[subject_id]['admissions']
                diagnoses_icd_data = subject_dict[subject_id]['diagnoses_icd']
                
                # 创建hadm_id到dischtime的映射
                hadm_to_dischtime = {}
                for admission in admissions_data:
                    if 'hadm_id' in admission and 'dischtime' in admission and admission['dischtime']:
                        try:
                            dischtime = pd.to_datetime(admission['dischtime'])
                            # 前一分钟
                            charttime = dischtime - timedelta(minutes=1)
                            hadm_to_dischtime[admission['hadm_id']] = charttime.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception as e:
                            print(f"Error processing dischtime for hadm_id {admission['hadm_id']}: {e}")
                
                # 为diagnoses_icd添加charttime列
                for diagnosis in diagnoses_icd_data:
                    if 'hadm_id' in diagnosis and diagnosis['hadm_id'] in hadm_to_dischtime:
                        diagnosis['charttime'] = hadm_to_dischtime[diagnosis['hadm_id']]
                    else:
                        diagnosis['charttime'] = ""  # 如果没有匹配，设为空
            
            # 需求（3）：为ed中的diagnosis增加charttime列
            if 'diagnosis' in subject_dict[subject_id] and 'edstays' in subject_dict[subject_id]:
                edstays_data = subject_dict[subject_id]['edstays']
                diagnoses_data = subject_dict[subject_id]['diagnosis']
                
                # 创建stay_id到outtime的映射
                stay_to_outtime = {}
                for edstay in edstays_data:
                    if 'stay_id' in edstay and 'outtime' in edstay and edstay['outtime']:
                        try:
                            outtime = pd.to_datetime(edstay['outtime'])
                            # 前一分钟
                            charttime = outtime - timedelta(minutes=1)
                            stay_to_outtime[edstay['stay_id']] = charttime.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception as e:
                            print(f"Error processing outtime for stay_id {edstay['stay_id']}: {e}")
                
                # 为diagnosis添加charttime列
                for diagnosis in diagnoses_data:
                    if 'stay_id' in diagnosis and diagnosis['stay_id'] in stay_to_outtime:
                        diagnosis['charttime'] = stay_to_outtime[diagnosis['stay_id']]
                    else:
                        diagnosis['charttime'] = ""  # 如果没有匹配，设为空
            
            # 需求（4）：将discharge中text的部分存入admissions
            if 'discharge' in subject_dict[subject_id] and 'admissions' in subject_dict[subject_id]:
                discharge_data = subject_dict[subject_id]['discharge']
                admissions_data = subject_dict[subject_id]['admissions']
                
                # 创建hadm_id到text的映射
                hadm_to_text = {}
                for discharge in discharge_data:
                    if 'hadm_id' in discharge and 'text' in discharge and discharge['text']:
                        text = discharge['text']
                        # 找到第一个"Physical Exam"之前的部分
                        physical_exam_index = text.find("Physical Exam")
                        if physical_exam_index != -1:
                            text_before_physical_exam = text[:physical_exam_index].strip()
                        else:
                            text_before_physical_exam = text.strip()
                        hadm_to_text[discharge['hadm_id']] = text_before_physical_exam
                
                # 为所有admissions填充text；无匹配的discharge text时置为空字符串（不再丢弃该subject）
                for admission in admissions_data:
                    if 'hadm_id' in admission:
                        admission['text'] = hadm_to_text.get(admission['hadm_id'], "")
        
        except Exception as e:
            print(f"Error processing subject {subject_id}: {e}")
            subjects_to_remove.append(subject_id)
    
    # 移除有问题的subject_id
    for subject_id in subjects_to_remove:
        del subject_dict[subject_id]
        print(f"Removed subject {subject_id} due to missing data matching")
    
    print(f"Preprocessing completed. Removed {len(subjects_to_remove)} subjects with incomplete data.")
    return subject_dict


def save_to_db(subject_dict, output_path='/remote-home/chuanxuan/datas/sample/db/patients'):
    
    for subject_id in tqdm.tqdm(list(subject_dict.keys())):
        # 确保父目录存在
        if not os.path.exists(output_path):
            try:
                os.makedirs(output_path)
                print(f"Created output directory: {output_path}")
            except Exception as e:
                print(f"Error creating output directory: {e}")
                return
        
        # patient_dir = os.path.join(output_path, str(subject_id))
        patient_dir = output_path
        
        # 确保患者目录存在
        if not os.path.exists(patient_dir):
            try:
                os.makedirs(patient_dir)
                print(f"Created patient directory: {patient_dir}")
            except Exception as e:
                print(f"Error creating patient directory: {e}")
                continue
        
        # 创建SQLite数据库
        db_path = os.path.join(patient_dir, f"patient_{subject_id}.db")
        try:
            # 配置SQLite以返回字典而不是元组
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row  # 这会让查询结果可以通过列名访问
            print(f"Successfully connected to database: {db_path}")
        except Exception as e:
            print(f"Error connecting to database {db_path}: {e}")
            continue
        
        # 为每个文件创建数据库表
        for file_name, records in subject_dict[subject_id].items():
            if not records:
                continue
            
            # 从第一条记录获取列名
            columns = list(records[0].keys())
            
            try:
                # 创建表
                columns_def = ", ".join([f'"{col}" TEXT' for col in columns])
                create_table_sql = f'CREATE TABLE IF NOT EXISTS "{file_name}" ({columns_def})'
                conn.execute(create_table_sql)
                
                # 插入数据
                for record in records:
                    placeholders = ", ".join(["?"] * len(columns))
                    column_names = ", ".join([f'"{col}"' for col in columns])
                    values = [str(record.get(col, "")) for col in columns]
                    insert_sql = f'INSERT INTO "{file_name}" ({column_names}) VALUES ({placeholders})'
                    conn.execute(insert_sql, values)
                
                print(f"Created table and inserted data for {file_name}")
                
                # 测试查询，验证数据以字典形式返回
                cursor = conn.cursor()
                cursor.execute(f'SELECT * FROM "{file_name}" LIMIT 1')
                row = cursor.fetchone()
                if row:
                    # 将sqlite3.Row对象转换为dict
                    row_dict = dict(row)
                    print(f"Sample row from {file_name} (as dict): {json.dumps(row_dict, cls=MyEncoder)[:100]}...")
                
            except Exception as e:
                print(f"Error working with table {file_name}: {e}")
        
        try:
            conn.commit()
            conn.close()
            print(f"Committed changes and closed database connection for subject_id: {subject_id}")
        except Exception as e:
            print(f"Error committing changes to database: {e}")
        
        print(f"Saved data for subject_id: {subject_id}")


# 添加一个辅助函数，用于从数据库中以JSON格式获取数据
def get_data_as_json(db_path, table_name):
    try:
        # 检查参数类型
        if isinstance(db_path, sqlite3.Connection):
            # 如果传入的是连接对象，直接使用
            conn = db_path
            should_close = False
        else:
            # 如果传入的是路径，创建新连接
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row  # 设置行工厂为sqlite3.Row
            should_close = True
        
        cursor = conn.cursor()
        cursor.execute(f'SELECT * FROM "{table_name}"')
        rows = cursor.fetchall()
        
        # 将sqlite3.Row对象列表转换为字典列表
        result = [dict(row) for row in rows]
        
        # 只有当我们创建了新连接时才关闭它
        if should_close:
            conn.close()
            
        return result
    except Exception as e:
        print(f"Error retrieving data from {db_path}, table {table_name}: {e}")
        return []
                            
                            
# def work(csv_path, file_name, index_csv, subject_dict, target_subject_ids=None):
   
#     with open(csv_path,'r') as f:
#         items = pd.read_csv(f, low_memory=False)  # 添加low_memory=False以避免DtypeWarning
    
#     # print(len(items))
#     # print(index_csv)

#     if "subject_id" in list(items.keys()):
#         print(f"Processing {index_csv}-th file {file_name} with {len(items)} items...")
#         # items['subject_id'] = items['subject_id'].astype(str)
        
#         # 如果指定了target_subject_id，只处理该ID的数据
#         if target_subject_ids is not None:
#             filtered_items = items[items["subject_id"].isin(target_subject_ids)]
            
#             if len(filtered_items) > 0:
#                 print(f"Found {len(filtered_items)} records for target_subject_ids in {file_name}...")
                
#                 for index in range(len(filtered_items)):
#                     sample = filtered_items.iloc[index].to_dict()
#                     subject_id = sample["subject_id"]
                    
#                     # 初始化subject_id的字典结构（如果不存在）
#                     if subject_id not in subject_dict:
#                         subject_dict[subject_id] = {}
                    
#                     # 初始化file_name的列表（如果不存在）
#                     if file_name not in subject_dict[subject_id]:
#                         subject_dict[subject_id][file_name] = []
                    
#                     # 添加样本到对应的文件名列表
#                     subject_dict[subject_id][file_name].append(sample)
#         # 如果未指定target_subject_id，处理所有数据
#         else:
#             for index in tqdm.tqdm(range(len(items))):
#                 sample = items.iloc[index].to_dict()
#                 subject_id = sample["subject_id"]
                
#                 # 初始化subject_id的字典结构（如果不存在）
#                 if subject_id not in subject_dict:
#                     subject_dict[subject_id] = {}
                
#                 # 初始化file_name的列表（如果不存在）
#                 if file_name not in subject_dict[subject_id]:
#                     subject_dict[subject_id][file_name] = []
                
#                 # 添加样本到对应的文件名列表
#                 subject_dict[subject_id][file_name].append(sample)


def work_optimized(csv_path, file_name, index_csv, subject_dict, target_subject_ids=None):
   
    # 1. 直接按路径读取，让 pandas 根据后缀自动识别压缩格式
    items = pd.read_csv(csv_path, low_memory=False)
    
    if "subject_id" not in items.keys():
        print(f"Warning: 'subject_id' column not found in {file_name}.")
        return

    print(f"Processing {index_csv}-th file {file_name} with {len(items)} items...")
    
    # 统一 subject_id 为字符串类型，确保分组和isin操作的一致性
    # items['subject_id'] = items['subject_id'].astype(str).str.strip()
    
    # 2. 过滤数据 (如果指定了 target_subject_ids)
    if target_subject_ids is not None:
        # target_subject_ids 转换为集合 (set) 以提高查找效率
        target_ids_set = set(target_subject_ids)
        items = items[items["subject_id"].isin(target_ids_set)]
        
        if len(items) == 0:
            print(f"No records found for target_subject_ids in {file_name}.")
            return
            
        print(f"Found {len(items)} records for target_subject_ids in {file_name}...")
    
    # 3. 高效分组和批量转换
    
    # 3.1. 使用 groupby('subject_id') 进行分组
    # 3.2. 对于每个 subject_id 组，使用 apply(lambda x: x.to_dict('records')) 将所有行批量转换为字典列表
    # 3.3. to_dict() 将结果转换为 {subject_id: list_of_dicts} 的字典
    
    grouped_data = items.groupby('subject_id').apply(
        lambda x: x.to_dict('records')
    ).to_dict()
    
    # 4. 批量更新 subject_dict
    
    # 遍历 grouped_data 的结果：{subject_id: list_of_all_samples}
    for subject_id, samples_list in grouped_data.items():
        
        # 初始化 subject_dict[subject_id] 的字典结构（如果不存在）
        if subject_id not in subject_dict:
            subject_dict[subject_id] = {}
            
        # 批量设置或覆盖 subject_dict[subject_id][file_name]
        # 注意：这里会覆盖 subject_dict[subject_id][file_name] 之前的内容，
        # 如果 file_name 在不同 csv_path 中多次出现，需要确认是否需要追加 (append) 而非覆盖 (overwrite)。
        # 根据你的原始代码逻辑，这里是覆盖（因为是 for 循环内部赋值），但通常更合理的做法是追加：
        
        # **原逻辑：覆盖**
        # subject_dict[subject_id][file_name] = samples_list
        
        # **更常见且合理的逻辑：追加 (推荐)**
        if file_name not in subject_dict[subject_id]:
            subject_dict[subject_id][file_name] = samples_list
        else:
            # 如果 file_name 已经存在，则将新样本追加到现有列表中
            subject_dict[subject_id][file_name].extend(samples_list)


def iter_table_files(dir_path):
    """
    遍历目录中的表文件，兼容 .csv / .csv.gz，并在两者同时存在时优先使用 .csv。
    """
    processed_table_names = set()

    for csv_file in sorted(os.listdir(dir_path), key=lambda name: (name.endswith(".csv.gz"), name)):
        if csv_file.endswith(".csv"):
            table_name = csv_file[:-4]
        elif csv_file.endswith(".csv.gz"):
            table_name = csv_file[:-7]
        else:
            continue

        if table_name in processed_table_names:
            continue

        processed_table_names.add(table_name)
        yield csv_file, table_name

def load_target_subject_ids(args):
    if args.subject_id is not None:
        target_subject_ids = [args.subject_id]
    
    elif args.data_file_path is not None:
        with open(args.data_file_path, "r") as f:
            datas = json.load(f)

        target_subject_ids = [data["subject_id"] for data in datas]
        exist_subject_ids = []
        for db_file in os.listdir(args.output_path):
            if db_file.startswith("patient") and db_file.endswith(".db"):
                exist_subject_ids.append(db_file.replace("patient_", "").replace(".db", ""))
            
        target_subject_ids = [subject_id for subject_id in target_subject_ids if str(subject_id) not in exist_subject_ids]
    
    elif args.data_dir_path is not None:
        datas = []
        for file in os.listdir(args.data_dir_path):
            if file.endswith(".json"):
                with open(os.path.join(args.data_dir_path, file), "r") as f:
                    datas += json.load(f)
        
        target_subject_ids = [data["subject_id"] for data in datas]
        exist_subject_ids = []
        for db_file in os.listdir(args.output_path):
            if db_file.startswith("patient") and db_file.endswith(".db"):
                exist_subject_ids.append(db_file.replace("patient_", "").replace(".db", ""))
            
        target_subject_ids = [subject_id for subject_id in target_subject_ids if str(subject_id) not in exist_subject_ids]
            

    else:
        target_subject_ids = None
    
    return target_subject_ids

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MIMIC-IV data preprocess')
    parser.add_argument('--root_path', type=str, default='/home/efs/zlt/datasets/MIMIC-IV/mimic_iv', help='Root path of MIMIC-IV subset data')
    parser.add_argument('--output_path', type=str, default='/home/efs/zlt/datasets/MIMIC-IV/database', help='Output path for processed data')
    parser.add_argument('--data_dirs', type=str, nargs='+', default=['ed', 'hosp', 'icu', 'note'], help='Directories containing CSV files')
    parser.add_argument('--data_file_path', type=str, default=None, help='Specific dataset in json formate to process the subject_id')
    parser.add_argument('--data_dir_path', type=str, default=None, help='Specific dataset in json formate to process the subject_id')
    parser.add_argument('--subject_id', type=int, default=None, help='Specific subject_id to process')
    args = parser.parse_args()
    
    root_path = args.root_path
    data_dirs = args.data_dirs
    target_subject_ids = load_target_subject_ids(args)

    subject_dict = {}
    
    if target_subject_ids is not None:
        print(f"Processing {len(target_subject_ids)} subject ID: {target_subject_ids}")
    else:
        print("Processing all subject IDs")
    
    index_csv = 0
    # 处理所有指定目录中的CSV文件
    for data_dir in data_dirs:
        dir_path = os.path.join(root_path, data_dir)
        if os.path.exists(dir_path):
            for csv_file, table_name in iter_table_files(dir_path):
                work_optimized(
                    csv_path=os.path.join(dir_path, csv_file), 
                    file_name=table_name, 
                    index_csv=index_csv, 
                    subject_dict=subject_dict,
                    target_subject_ids=target_subject_ids
                )
                index_csv += 1
        else:
            print(f"Directory not found: {dir_path}")

    # if target_subject_id is not None and target_subject_id not in subject_dict:
    if target_subject_ids is None:
        target_subject_ids = subject_dict.keys()

    # 在保存到数据库之前进行预处理
    print("Starting data preprocessing...")
    subject_dict = preprocess_subject_dict(subject_dict)
    
    if subject_dict:  # 只有在有数据时才保存
        save_to_db(subject_dict=subject_dict, output_path=args.output_path)
        print(f"Successfully processed and saved data for subject_id")
    else:
        print("No valid data found after preprocessing.")
    
