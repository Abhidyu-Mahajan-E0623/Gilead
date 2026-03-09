import json

file_path = "c:\\Users\\AbhidyuMahajan\\OneDrive - ProcDNA Analytics Pvt. Ltd\\Desktop\\Projects\\Gilead_Demo\\Input\\GILEAD_Field_Inquiry_Playbook.json"
orig_file_path = "c:\\Users\\AbhidyuMahajan\\OneDrive - ProcDNA Analytics Pvt. Ltd\\Desktop\\Projects\\Gilead_Demo\\Input\\GILEAD_Field_Inquiry_Playbook.json.bak"

import shutil
try:
    shutil.copyfile(orig_file_path, file_path) # Restoring the original non-bulleted version back
except:
    pass

with open(file_path, "r", encoding="utf-8") as f:
    data = json.load(f)

for item in data.get("inquiries", []):
    inquiry_id = item.get("inquiry_id")

    # The user specifically provided two hardcoded exact strings they want to see, plus a note to remove em-dashes for all.
    # We will manually replace inquiry 09 and 12 with the exact strings, and leave others grouped into paragraphs if possible, or just strip em dashes for the rest.
    
    if inquiry_id == "09":
        new_res = (
            "We confirmed Dr. Rodriguez's NPI (1990348752) is active in the NPI Registry. Her specialty is Infectious Disease (code 44) and her primary practice address is confirmed at the County HIV Treatment Center.\n\n"
            "IQVIA OneKey has no existing record for this NPI, she is a genuine gap in the master data, not a duplicate or name mismatch.\n\n"
            "A new HCP onboarding request has been submitted to IQVIA with her full profile. DCR #DCR-2024-1364 has been logged to prioritize the onboarding given her Infectious Disease specialty and active HIV patient population.\n\n"
            "Once her OneKey record publishes, the CRM profile will be created automatically with specialty code 44, which will trigger the Credit flag for Biktarvy, Sunlenca, and Descovy treatment from her first record load.\n\n"
            "Estimated timeline: Dr. Rodriguez's CRM profile should be available within 3-5 business days. We have also notified the segmentation team to include her in the next targeting review for potential Tier 1 placement.\n\n"
            "In the interim, any DDD prescribing activity captured under her NPI during this period will be retroactively attributed once her profile is live."
        )
        item["resolution_and_response_to_rep"] = new_res
        
    elif inquiry_id == "12":
        new_res = (
            "Your dashboard is displaying data through the 867 load dated 03/01, the final two weeks of your reporting period have not yet loaded. This is a timing lag, not missing data.\n\n"
            "We also identified that one of your prescribers switched specialty pharmacies. Their new pharmacy's distributor feed is not yet mapped to your territory, DCR #DCR-2024-1304 has been submitted to complete that mapping and backfill transactions.\n\n"
            "We reviewed the specialty and Credit flag status of your three cited prescribers: two are Infectious Disease physicians (code 44, Credit, fully counts toward IC) and one is an Internal Medicine physician (code 11, Credit, fully counts toward IC). All three are creditable. The full gap you are experiencing is a timing and feed mapping issue, none of it is non-creditable specialty volume.\n\n"
            "Your dashboard will reflect the complete period data within 5-7 business days once the feed mapping correction processes. Estimated correctable volume is approximately 18 units of Biktarvy."
        )
        item["resolution_and_response_to_rep"] = new_res
        
    else:
        resolution = item.get("resolution_and_response_to_rep", "")
        if resolution:
            resolution = resolution.replace("\n", " ").replace("• ", " ")
            resolution = " ".join(resolution.split())
            
            # replace dashes
            resolution = resolution.replace(" — ", ", ").replace(" - ", ", ").replace("—",", ").replace("- ",", ")
            
            item["resolution_and_response_to_rep"] = resolution

with open(file_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print("Successfully replaced with the explicit pointers format.")
