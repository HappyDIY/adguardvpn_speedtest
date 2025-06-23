#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import subprocess
import re
import csv
import logging
from datetime import datetime
import speedtest
import shutil
from pathlib import Path
import socket
import socks
import urllib.request

# --- 配置常量 ---
ADGUARD_CLI_PATH = "/opt/adguardvpn_cli/adguardvpn-cli"
SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 1080
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
RESULTS_CSV_FILE = f"adguard_speedtest_results_{TIMESTAMP}.csv"
LOG_FILE = f"adguard_speedtest_{TIMESTAMP}.log"

# --- 日志设置 ---
def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

# --- 核心功能函数 ---
def run_command(command, check=True):
    logging.info(f"执行命令: {' '.join(command)}")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=check, encoding='utf-8'
        )
        if result.stderr: logging.debug(f"命令 stderr: \n{result.stderr.strip()}")
        logging.debug(f"命令 stdout: \n{result.stdout.strip()}")
        return result.stdout
    except FileNotFoundError:
        logging.error(f"命令未找到: {command[0]}。")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logging.error(f"命令执行失败: {' '.join(command)}")
        logging.error(f"返回码: {e.returncode}\nStdout: \n{e.stdout.strip()}\nStderr: \n{e.stderr.strip()}")
        raise
    except Exception as e:
        logging.error(f"执行命令时发生未知错误: {e}")
        raise

def get_locations():
    logging.info("--- 步骤 2: 获取所有可用节点信息 ---")
    output = run_command([ADGUARD_CLI_PATH, "list-locations"])
    locations = []
    location_pattern = re.compile(r"^\s*([A-Z]{2})\s+(.+?)\s{2,}(.+?)\s{2,}(\d+)\s*$")
    for line in output.splitlines():
        match = location_pattern.match(line)
        if match:
            iso, country, city, ping = match.groups()
            locations.append({"ISO": iso.strip(), "Country": country.strip(), "City": city.strip(), "Ping Estimate": ping.strip()})
    if not locations:
        logging.error("未能解析出任何节点信息。")
        sys.exit(1)
    logging.info(f"成功获取 {len(locations)} 个VPN节点信息。")
    return locations

def load_tested_nodes():
    tested_nodes = set()
    result_files = [f for f in os.listdir('.') if f.startswith('adguard_speedtest_results_') and f.endswith('.csv')]
    if not result_files:
        logging.info("未找到旧的结果文件，将开始全新的测试。")
        return tested_nodes
    latest_file = max(result_files, key=lambda f: os.path.getmtime(f))
    global RESULTS_CSV_FILE
    RESULTS_CSV_FILE = latest_file
    logging.info(f"检测到存在的结果文件: {latest_file}，将在此文件上继续测试。")
    try:
        with open(latest_file, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if 'ISO' in row and row['ISO']:
                    tested_nodes.add(row['ISO'])
        logging.info(f"已从 '{latest_file}' 加载 {len(tested_nodes)} 个已测试节点。")
    except Exception as e:
        logging.warning(f"读取旧结果文件时出错: {e}。将开始全新测试。")
    return tested_nodes

def test_and_record_speed(location, csv_writer, is_new_file):
    iso = location['ISO']
    logging.info(f"--- 开始测试节点: {location['City']}, {location['Country']} ({iso}) ---")
    try:
        run_command([ADGUARD_CLI_PATH, "connect", "-l", iso], check=True)
        logging.info(f"成功连接到节点: {iso}。SOCKS代理已在 {SOCKS_HOST}:{SOCKS_PORT} 启动。")
    except subprocess.CalledProcessError:
        logging.error(f"连接到节点 {iso} 失败。跳过。")
        return
    
    speed_results = {}
    # 保存原始socket设置
    original_socket = socket.socket
    
    try:
        # 设置SOCKS代理
        socks.set_default_proxy(socks.SOCKS5, SOCKS_HOST, SOCKS_PORT)
        socket.socket = socks.socksocket
        
        logging.info("正在初始化speedtest客户端...")
        s = speedtest.Speedtest(timeout=30, secure=True)
        
        logging.info("正在获取测速服务器...")
        s.get_servers()
        s.get_best_server()
        
        logging.info("正在进行下载/上传测速...")
        s.download(threads=None)
        s.upload(threads=None)
        
        speed_results = s.results.dict()
        dl = speed_results.get('download', 0)/10**6
        ul = speed_results.get('upload', 0)/10**6
        png = speed_results.get('ping', 0)
        
        logging.info(f"测速完成: 下载={dl:.2f} Mbps, 上传={ul:.2f} Mbps, Ping={png:.2f} ms")
        
    except Exception as e:
        logging.error(f"节点 {iso} 测速时发生错误: {e}", exc_info=True)
        
    finally:
        # 恢复原始socket设置
        socket.socket = original_socket
        socks.set_default_proxy()
        
        logging.info(f"断开节点 {iso} 的连接...")
        run_command([ADGUARD_CLI_PATH, "disconnect"], check=False)
    
    if not speed_results:
        return
    
    full_result = {**location, **speed_results}
    if is_new_file[0]:
        csv_writer.fieldnames = list(full_result.keys())
        csv_writer.writeheader()
        is_new_file[0] = False
    csv_writer.writerow(full_result)
    logging.info(f"结果已保存到: {RESULTS_CSV_FILE}")

# --- 主逻辑 ---
def main():
    setup_logging()
    logging.info("====== AdGuard VPN 节点自动测速脚本启动 ======")

    if os.geteuid() != 0:
        logging.error("此脚本需要以root权限运行。请使用 'sudo python3 <script_name>.py'。")
        sys.exit(1)
        
    original_user = os.getenv('SUDO_USER')
    if not original_user:
        logging.error("无法确定原始用户名。请确保您是通过 'sudo' 而不是直接以root用户登录来运行此脚本。")
        sys.exit(1)
    
    source_config_path = Path(f"/home/{original_user}/.local/share/adguardvpn-cli")
    dest_config_path = Path("/root/.local/share/adguardvpn-cli")
    dest_backup_path = dest_config_path.with_suffix('.bak')

    if not source_config_path.is_dir():
        logging.error(f"在 {source_config_path} 未找到您的AdGuard VPN配置文件。")
        print("\n请先以您的普通用户身份登录AdGuard VPN：")
        print(f"    {ADGUARD_CLI_PATH} login")
        print("登录成功后，再重新使用 'sudo' 运行此脚本。")
        sys.exit(1)
    
    try:
        logging.info(f"检测到原始用户: {original_user}。准备临时使用其配置文件。")
        if dest_config_path.exists():
            logging.info(f"为root用户的现有配置创建备份: {dest_backup_path}")
            shutil.move(str(dest_config_path), str(dest_backup_path))

        logging.info(f"正在将配置从 {source_config_path} 复制到 {dest_config_path}...")
        shutil.copytree(
            source_config_path, 
            dest_config_path,
            ignore=shutil.ignore_patterns('*.socket')
        )
        
        logging.info("--- 步骤 1: 将AdGuard VPN切换至SOCKS代理模式 ---")
        run_command([ADGUARD_CLI_PATH, "config", "set-mode", "socks"])
        
        all_locations = get_locations()
        tested_nodes = load_tested_nodes()
        is_new_file = [not os.path.exists(RESULTS_CSV_FILE) or os.path.getsize(RESULTS_CSV_FILE) == 0]
        
        with open(RESULTS_CSV_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[], extrasaction='ignore')
            
            if not is_new_file[0]:
                with open(RESULTS_CSV_FILE, 'r', newline='', encoding='utf-8') as read_f:
                    reader = csv.reader(read_f)
                    try:
                        headers = next(reader)
                        writer.fieldnames = headers
                    except StopIteration:
                        is_new_file[0] = True

            logging.info("--- 步骤 3: 开始循环测速 ---")
            total = len(all_locations)
            for i, location in enumerate(all_locations):
                if location['ISO'] in tested_nodes:
                    logging.info(f">>> ({i+1}/{total}) 跳过已测试节点: {location['City']} ({location['ISO']})")
                    continue
                logging.info(f"\n>>> 处理进度: {i+1}/{total} | 节点: {location['City']} ({location['ISO']})")
                test_and_record_speed(location, writer, is_new_file)
                f.flush()

    except (KeyboardInterrupt, SystemExit) as e:
        logging.warning(f"\n脚本被中断 ({type(e).__name__})。")
    except Exception as e:
        logging.critical(f"脚本发生严重错误，意外终止: {e}", exc_info=True)
    finally:
        logging.info("--- 清理临时配置文件 ---")
        if dest_config_path.exists():
            logging.info(f"移除临时复制的配置: {dest_config_path}")
            shutil.rmtree(dest_config_path)
        if dest_backup_path.exists():
            logging.info(f"恢复root用户的原始配置: {dest_config_path}")
            shutil.move(str(dest_backup_path), str(dest_config_path))
        
        logging.info("正在确保VPN已断开...")
        run_command([ADGUARD_CLI_PATH, "disconnect"], check=False)
        logging.info("====== 脚本执行结束 ======")

if __name__ == "__main__":
    main()