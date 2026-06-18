import pandas as pd
import requests

df = pd.read_csv("app_categories2.csv")

for index, row in df.iterrows():
    package_name = row["App Id"]
    category_name = row["Category"]

    if "VIDEO_PLAYERS" in category_name:
        #df.loc[index, "Category"] = "SOCIAL"
        response = requests.post(
            "https://mdm-backend-i4b0.onrender.com/api/app-category",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": "api_3d9a7c1f5b824e9aa4d6f7c8b1e2a3d4"
            },
            json={
                "packageName": package_name
            }
        )
        data = response.json()
        df.loc[index, "Category"] = data["category"]
        print(data["category"])

df.to_csv("app_categories2.csv", index=False)