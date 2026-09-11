import os
import random
import socket
import time
import re
import requests
import concurrent.futures
from datetime import datetime, timedelta, timezone

# ==========================================
# 🎯 全局默认地区设置 (如果想要永久换地区，只改这里！)
# 支持多个地区，用逗号隔开，例如 "SJC,LAX,HKG,FRA,NRT"
# 💡 新手不知道有什么地区？可以直接填 "ALL"，系统会全区盲扫并自动创建所有能扫到的地区子域名！
# ==========================================
DEFAULT_REGIONS = "SJC"

# 🌐 主域名终极大汇总同步开关
# 设置为 "YES": 开启！将所有扫到的极品节点汇总推送到你的主域名（全球负载均衡）
# 设置为 "NO": 关闭！仅同步到各个地区子域名，不修改主域名的解析记录
SYNC_MAIN_DOMAIN = "NO"

# 🎯 扫描与同步数量设置
# 控制每个地区最终要同步几个 IP 到 Cloudflare DNS (默认 10 个)
SYNC_COUNT = 10
# ==========================================

import ipaddress

def generate_ips(target_regions, is_scan_all, ips_v4_file="ips-v4.txt", ip_txt_file="ip.txt"):
    history_ips = []
    
    # 第一步：先扫描 ips-v4.txt 中的历史优秀单 IP
    print("Stage 1: Scanning historical excellent single IPs...")
    if os.path.exists(ips_v4_file):
        try:
            with open(ips_v4_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ip = line.split("#")[0]
                        history_ips.append(ip)
                        yield ip
        except Exception as e:
            print(f"Error reading {ips_v4_file}: {e}")

    # 第二步：智能匹配 good_subnets.txt 中已标记的优秀地区段
    print("Stage 2: Scanning learned good subnets by region...")
    good_cidrs_scanned = set()
    if os.path.exists("good_subnets.txt"):
        try:
            with open("good_subnets.txt", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and "#" in line:
                        cidr, colo = line.split("#", 1)
                        colo = colo.upper()
                        # 如果需要扫描所有地区，或者这个段的地区在我们需要的列表中，就优先扫它
                        if is_scan_all or colo in target_regions:
                            good_cidrs_scanned.add(cidr)
                            try:
                                net = ipaddress.ip_network(cidr, strict=False)
                                for ip_obj in net:
                                    yield str(ip_obj)
                            except Exception:
                                pass
        except Exception as e:
            print(f"Error reading good_subnets.txt: {e}")

    # 第三步：将历史单 IP 扩展为 /24 C段继续扫描（跳过第二步已经扫过的）
    derived_cidrs = set()
    for ip in history_ips:
        parts = ip.split('.')
        if len(parts) == 4:
            cidr = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
            if cidr not in good_cidrs_scanned:
                derived_cidrs.add(cidr)
            
    if derived_cidrs:
        print(f"Stage 3: Scanning {len(derived_cidrs)} derived /24 subnets from history...")
        for cidr in derived_cidrs:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                for ip_obj in net:
                    yield str(ip_obj)
            except Exception:
                pass

    # 第四步：如果配额依然没满，按照 ip.txt 中的段进行兜底轮巡扫描
    print("Stage 4: Scanning full IP database from ip.txt...")
    if os.path.exists(ip_txt_file):
        try:
            with open(ip_txt_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        cidr = line
                        try:
                            if '/' in cidr:
                                net = ipaddress.ip_network(cidr, strict=False)
                                for ip_obj in net:
                                    yield str(ip_obj)
                            else:
                                yield cidr
                        except Exception:
                            pass
        except Exception as e:
            print(f"Error reading {ip_txt_file}: {e}")


def test_ip(ip, check_api_url, timeout=5.0):
    start_time = time.time()
    try:
        url = f"{check_api_url}?proxyip={ip}"
        
        resp = requests.get(url, timeout=timeout).json()
        if resp.get("success") is True:
            connect_time = int((time.time() - start_time) * 1000)
            
            # 提取数据中心 (dataCenter)、colo 或 country，优先用 dataCenter
            colo = resp.get("dataCenter") or resp.get("colo") or resp.get("country") or "UNK"
            
            # 如果 API 返回了 latencyMs 或者 latency，优先用 API 测算的延迟，否则用整个请求的耗时
            latency = resp.get("latencyMs") or resp.get("tcpDuration") or connect_time
            
            return {"ip": ip, "latency": latency, "colo": colo}
    except Exception:
        pass
    return None

def sync_to_cloudflare(api_token, zone_id, target_domain, best_ips, cf_email):
    headers = {
        "X-Auth-Email": cf_email,
        "X-Auth-Key": api_token,
        "Content-Type": "application/json"
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=A&name={target_domain}"
    
    print(f"Fetching existing DNS records for {target_domain}...")
    try:
        resp = requests.get(url, headers=headers).json()
        if not resp.get("success"):
            print("Failed to fetch DNS records:", resp)
            return False
        
        existing_records = resp.get("result", [])
        existing_map = {r["content"]: r["id"] for r in existing_records}
        desired_ips = [ip["ip"] for ip in best_ips]
        
        # 1. Delete records that are no longer in our best_ips list
        for ip_val, record_id in existing_map.items():
            if ip_val not in desired_ips:
                print(f"Deleting outdated IP: {ip_val}")
                del_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
                requests.delete(del_url, headers=headers)
                
        # 2. Add new IPs
        for ip_val in desired_ips:
            if ip_val not in existing_map:
                print(f"Adding new IP: {ip_val}")
                post_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
                data = {
                    "type": "A",
                    "name": target_domain,
                    "content": ip_val,
                    "ttl": 60,  # Auto/1 minute
                    "proxied": False
                }
                requests.post(post_url, headers=headers, json=data)
                
        print("Cloudflare DNS Sync completed successfully!")
        return True
    except Exception as e:
        print(f"Exception during Cloudflare sync: {e}")
        return False

def save_ips_to_file(best_ips):
    # Calculate Beijing Time (UTC+8)
    bj_time = datetime.now(timezone.utc) + timedelta(hours=8)
    time_str = bj_time.strftime("%Y-%m-%d %H:%M:%S")
    
    # Save the individual IPs
    with open("ips-v4.txt", "w", encoding="utf-8") as f:
        # 写入纯 IP 和 地区备注，格式为 IP#地区
        # 很多代理/机场客户端使用 # 作为节点备注的分隔符
        for ip in best_ips:
            f.write(f"{ip['ip']}#{ip['colo']}\n")
            
    print("Successfully saved latest IPs to ips-v4.txt")

    # Save the successful /24 subnets persistently for future prioritized scanning
    subnets = set()
    if os.path.exists("good_subnets.txt"):
        try:
            with open("good_subnets.txt", "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        subnets.add(line.strip())
        except Exception:
            pass

    new_subnets = 0
    for ip in best_ips:
        parts = ip['ip'].split('.')
        if len(parts) == 4:
            cidr = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
            entry = f"{cidr}#{ip['colo']}"
            if entry not in subnets:
                subnets.add(entry)
                new_subnets += 1

    if new_subnets > 0:
        try:
            with open("good_subnets.txt", "w", encoding="utf-8") as f:
                for entry in sorted(subnets):
                    f.write(f"{entry}\n")
            print(f"Successfully learned and saved {new_subnets} new good subnets to good_subnets.txt")
        except Exception as e:
            print(f"Error saving subnets: {e}")

def main():
    api_token = os.environ.get("CF_API_TOKEN")
    zone_id = os.environ.get("CF_ZONE_ID")
    base_domain = os.environ.get("CF_TARGET_DOMAIN")
    cf_email = os.environ.get("CF_EMAIL")
    
    region_input = DEFAULT_REGIONS
    target_regions = [r.strip().upper() for r in region_input.split(",") if r.strip()]
    is_scan_all = "ALL" in target_regions
    
    if is_scan_all:
        print(f"Target Regions dynamically set to: ALL (Global Scan Mode)")
    else:
        print(f"Target Regions dynamically set to: {target_regions}")
    
    check_api_url = "https://pagesip.woxxxxxx.nyc.mn/check"
    sync_count = SYNC_COUNT
    
    can_sync = True
    if not all([api_token, zone_id, base_domain, cf_email]):
        print("Warning: Missing required environment variables (CF_API_TOKEN, CF_ZONE_ID, CF_TARGET_DOMAIN, CF_EMAIL).")
        print("DNS Synchronization will be skipped, but IP scanning will still proceed!")
        can_sync = False
        
    print(f"Starting sequential IP scan...")
    print(f"Testing IPs concurrently via {check_api_url}...")
    
    valid_ips_by_region = {}
    if not is_scan_all:
        valid_ips_by_region = {region: [] for region in target_regions}
    
    ALL_MODE_LIMIT = 20
    
    def quotas_met():
        if is_scan_all:
            total_collected = sum(len(ips) for ips in valid_ips_by_region.values())
            return total_collected >= ALL_MODE_LIMIT
        else:
            return all(len(ips) >= sync_count for ips in valid_ips_by_region.values())

    tested_ips = set()
    ip_generator = generate_ips(target_regions=target_regions, is_scan_all=is_scan_all)
    
    # === 并发线程配置区 ===
    # 控制同时发起多少个测速请求，默认 50，太高容易导致测速接口崩溃
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        futures = {}
        
        def submit_next_batch(num):
            count = 0
            for ip in ip_generator:
                if ip not in tested_ips:
                    tested_ips.add(ip)
                    futures[executor.submit(test_ip, ip, check_api_url)] = ip
                    count += 1
                    if count >= num:
                        break
                        
        # 初始提交一批任务
        submit_next_batch(200)
        
        total_tested = 0
        while futures:
            done, not_done = concurrent.futures.wait(futures.keys(), return_when=concurrent.futures.FIRST_COMPLETED)
            
            for future in done:
                ip = futures.pop(future)
                total_tested += 1
                if total_tested % 50 == 0:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Tested {total_tested} IPs so far...")
                    
                try:
                    result = future.result()
                    if result:
                        colo = result.get('colo', 'UNK').upper()
                        if colo != 'UNK' and (is_scan_all or colo in target_regions):
                            if colo not in valid_ips_by_region:
                                valid_ips_by_region[colo] = []
                                
                            if is_scan_all:
                                total_collected = sum(len(ips) for ips in valid_ips_by_region.values())
                                if total_collected < ALL_MODE_LIMIT:
                                    valid_ips_by_region[colo].append(result)
                                    print(f"[FOUND {colo}] {result['ip']} (Total ALL: {total_collected + 1}/{ALL_MODE_LIMIT})")
                            else:
                                if len(valid_ips_by_region[colo]) < sync_count:
                                    valid_ips_by_region[colo].append(result)
                                    print(f"[FOUND {colo}] {result['ip']} (Total {colo}: {len(valid_ips_by_region[colo])}/{sync_count})")
                except Exception:
                    pass
                    
            if quotas_met():
                print("All regional quotas met! Stopping scan early.")
                executor.shutdown(wait=False, cancel_futures=True)
                break
                
            # 继续补充任务以维持并发量
            submit_next_batch(len(done))
            
    print("\nScan completed. Summary:")
    total_found = 0
    all_best_ips = []
    
    for region, ips in valid_ips_by_region.items():
        print(f"- {region}: {len(ips)} valid IPs found")
        if not ips:
            print(f"  Warning: No IPs found for {region}")
            continue
            
        total_found += len(ips)
        
        # Sort by latency (lowest first)
        ips.sort(key=lambda x: x["latency"])
        
        # Take the top fastest ones
        limit = ALL_MODE_LIMIT if is_scan_all else sync_count
        best_ips = ips[:limit]
        all_best_ips.extend(best_ips)
        
        print(f"\n--- Top {len(best_ips)} IPs Selected for {region} ---")
        for ip in best_ips:
            print(f"IP: {ip['ip']:<15} | Latency: {ip['latency']:>3}ms | Colo: {ip['colo']}")
            
        # Target domain specific to this region
        if can_sync:
            target_domain = f"{region.lower()}.{base_domain}"
            print(f"\nStarting Cloudflare DNS Sync for {target_domain}...")
            sync_to_cloudflare(api_token, zone_id, target_domain, best_ips, cf_email)
        else:
            print(f"\nSkipping Cloudflare DNS Sync for {region} (Missing Credentials).")
                
    if can_sync and all_best_ips:
        if SYNC_MAIN_DOMAIN.strip().upper() == "YES":
            all_best_ips.sort(key=lambda x: x["latency"])
            print(f"\n[Global Sync] Starting Cloudflare DNS Sync for MAIN DOMAIN: {base_domain}")
            sync_to_cloudflare(api_token, zone_id, base_domain, all_best_ips, cf_email)
        else:
            print(f"\n[Global Sync] Skipped synchronizing to MAIN DOMAIN ({base_domain}) because SYNC_MAIN_DOMAIN is set to NO.")

    if total_found == 0:
        print("No valid IPs found in this scan across any regions. Aborting.")
        exit(1)
        
    # Save ALL best IPs from all regions to the text file for next run's subnet learning
    if all_best_ips:
        save_ips_to_file(all_best_ips)

if __name__ == "__main__":
    main()
