import os
import random
import time
import requests
import concurrent.futures

def load_cf_cidrs(file_path="ip.txt"):
    if not os.path.exists(file_path):
        print(f"Error: {file_path} not found.")
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

def get_random_ips_from_cidr(cidr, count=2):
    ips = []
    try:
        if '/' in cidr:
            base_ip, prefix = cidr.split('/')
            prefix = int(prefix)
        else:
            base_ip = cidr
            prefix = 32
            
        parts = list(map(int, base_ip.split('.')))
        if len(parts) != 4:
            return ips
            
        ip_long = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
        host_bits = 32 - prefix
        mask = (1 << host_bits) - 1
        
        for _ in range(count):
            random_host = random.randint(0, mask)
            final_ip_long = (ip_long & ~mask) | random_host
            p1 = (final_ip_long >> 24) & 255
            p2 = (final_ip_long >> 16) & 255
            p3 = (final_ip_long >> 8) & 255
            p4 = final_ip_long & 255
            ips.append(f"{p1}.{p2}.{p3}.{p4}")
    except Exception:
        pass
    return ips

def test_ip(ip, check_api_url, timeout=5.0):
    try:
        url = f"{check_api_url}?proxyip={ip}"
        resp = requests.get(url, timeout=timeout).json()
        if resp.get("success") is True:
            colo = resp.get("dataCenter") or resp.get("colo") or resp.get("country") or "UNK"
            return {"ip": ip, "colo": colo.upper()}
    except Exception:
        pass
    return None

def main():
    check_api_url = "https://proxyip.xxxxxxx.nyc.mn/check"
    cidrs = load_cf_cidrs("ip.txt")
    if not cidrs:
        print("No CIDRs found in ip.txt")
        return
        
    print(f"Loaded {len(cidrs)} CIDRs from ip.txt. Starting global scan...")
    
    # 随机打乱扫描顺序
    random.shuffle(cidrs)
    
    active_subnets = set()
    
    # 读取已有的热点段，避免重复工作
    log_file = "hot_cidrs.txt"
    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    active_subnets.add(line)
                    
    initial_count = len(active_subnets)
    print(f"Loaded {initial_count} existing active subnets from {log_file}")
    
    max_workers = 100
    futures_map = {}
    
    # 开始提交并发任务
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for cidr in cidrs:
            ips_to_test = get_random_ips_from_cidr(cidr, count=2)
            for ip in ips_to_test:
                futures_map[executor.submit(test_ip, ip, check_api_url)] = ip

        print(f"Submitted {len(futures_map)} IP test tasks. Waiting for responses...")
        
        completed = 0
        total = len(futures_map)
        for future in concurrent.futures.as_completed(futures_map):
            completed += 1
            if completed % 2000 == 0:
                print(f"Progress: {completed}/{total} IPs tested...")
                
            try:
                result = future.result()
                if result:
                    colo = result['colo']
                    ip = result['ip']
                    parts = ip.split('.')
                    if len(parts) == 4:
                        prefix24 = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
                        cidr_str = f"{prefix24}#{colo}"
                        if cidr_str not in active_subnets:
                            active_subnets.add(cidr_str)
                            print(f"[NEW ACTIVE SUBNET] {cidr_str} (from IP {ip})")
            except Exception:
                pass
                
    new_count = len(active_subnets)
    print(f"\nScan complete! Found {new_count - initial_count} new subnets.")
    print(f"Total active subnets in database: {new_count}")
    
    # 保存结果
    with open(log_file, "w", encoding="utf-8") as f:
        for cidr in sorted(list(active_subnets)):
            f.write(f"{cidr}\n")
            
    print(f"Successfully saved all active subnets to {log_file}")

if __name__ == "__main__":
    main()
