const express = require('express');
const axios = require('axios');
const app = express();

// Required if you plan to send dynamic member IDs from the app to the backend later
app.use(express.json());

app.put('/api/move-member-to-official', async (req, res) => {
    // ADD YOUR APP'S AUTHENTICATION CHECK HERE FIRST!
    // Do not leave this open to the public internet without checking a header/token from your APK.
    const memberId = req.body.memberId;
    if (!memberId) {
        return res.status(400).send("Missing memberId");
    }
    try {
        // Step 1: Generate a fresh Access Token using the permanent Refresh Token
        const tokenResponse = await axios.post('https://accounts.zoho.com/oauth/v2/token', null, {
            params: {
                refresh_token: process.env.ZOHO_REFRESH_TOKEN,
                client_id: process.env.ZOHO_CLIENT_ID,
                client_secret: process.env.ZOHO_CLIENT_SECRET,
                grant_type: 'refresh_token'
            }
        });

        const newAccessToken = tokenResponse.data.access_token;

        if (!newAccessToken) {
            console.error("Token Response:", tokenResponse.data);
            return res.status(500).send("Failed to get fresh access token from Zoho.");
        }

        /// Step 2: Move device from Kiosk Group to Official Group
        const mdmResponse = await axios.put(
            `https://mdm.manageengine.com/api/v1/mdm/groups/${process.env.KIOSK_GROUP_ID}/targetgroups`,
            {
                member_ids: [Number(memberId)],
                target_group_ids: [Number(process.env.OFFICIAL_GROUP_ID)]
            },
            {
                headers: {
                    'Authorization': `Zoho-oauthtoken ${newAccessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        res.status(200).json(mdmResponse.data);

    } catch (error) {
        // Log the actual error to your server console so you can debug it in Render
        console.error("API Error:", error.response?.data || error.message);
        res.status(error.response?.status || 500).send("API Call Failed");
    }
});



app.put('/api/move-member-to-kiosk', async (req, res) => {
    // ADD YOUR APP'S AUTHENTICATION CHECK HERE FIRST!
    // Do not leave this open to the public internet without checking a header/token from your APK.
    const memberId = req.body.memberId;
    if (!memberId) {
        return res.status(400).send("Missing memberId");
    }
    try {
        // Step 1: Generate a fresh Access Token using the permanent Refresh Token
        const tokenResponse = await axios.post('https://accounts.zoho.com/oauth/v2/token', null, {
            params: {
                refresh_token: process.env.ZOHO_REFRESH_TOKEN,
                client_id: process.env.ZOHO_CLIENT_ID,
                client_secret: process.env.ZOHO_CLIENT_SECRET,
                grant_type: 'refresh_token'
            }
        });

        const newAccessToken = tokenResponse.data.access_token;

        if (!newAccessToken) {
            console.error("Token Response:", tokenResponse.data);
            return res.status(500).send("Failed to get fresh access token from Zoho.");
        }

        /// Step 2: Move device from Official Group to Kiosk Group
        const mdmResponse = await axios.put(
            `https://mdm.manageengine.com/api/v1/mdm/groups/${process.env.OFFICIAL_GROUP_ID}/targetgroups`,
            {
                member_ids: [Number(memberId)],
                target_group_ids: [Number(process.env.KIOSK_GROUP_ID)]
            },
            {
                headers: {
                    'Authorization': `Zoho-oauthtoken ${newAccessToken}`,
                    'Content-Type': 'application/json'
                }
            }
        );
        
        res.status(200).json(mdmResponse.data);

    } catch (error) {
        // Log the actual error to your server console so you can debug it in Render
        console.error("API Error:", error.response?.data || error.message);
        res.status(error.response?.status || 500).send("API Call Failed");
    }
});


// Start the server
app.listen(process.env.PORT || 3000, () => {
    console.log("Server running...");
});