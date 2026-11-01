# Dashboard Studio for Home Assistant

Beautiful dashboards for Home Assistant wall tablets: a glass look, large calm type, and rooms first. This integration serves the dashboard to your tablets and keeps your layout. Design it in the web studio inside Home Assistant, or in the [Dashboard Studio Mac app](https://lukaps.com/dashboardstudio/).

![A Dashboard Studio dashboard on a 10-inch tablet](https://raw.githubusercontent.com/Radushka/ha-dashboard-studio/main/images/dashboard.jpg)

## Install in Home Assistant 2026.11 or later (Marketplace)

1. Go to **Settings > Marketplace**.
2. Add `https://github.com/Radushka/ha-dashboard-studio` as a custom repository (Home Assistant asks you to sign in to GitHub once for this).
3. Find **Dashboard Studio** in the Marketplace and install it. Restart Home Assistant if it asks you to.
4. Go to **Settings > Devices & services > Add integration** and add **Dashboard Studio**.

## Install in Home Assistant 2026.10 (HACS)

1. In HACS, open the menu (⋮) and choose **Custom repositories**.
2. Add `https://github.com/Radushka/ha-dashboard-studio` with the type **Integration**.
3. Find **Dashboard Studio** in HACS and download it.
4. Restart Home Assistant.
5. Go to **Settings > Devices & services > Add integration** and add **Dashboard Studio**.

If you update from 2026.10 to 2026.11, you do not need to do anything. Home Assistant moves Dashboard Studio from HACS to the Marketplace by itself, and updates keep working.

## Install by hand

1. Download `hadash.zip` from the newest [release](https://github.com/Radushka/ha-dashboard-studio/releases).
2. Unpack it into `config/custom_components/hadash/`.
3. Restart Home Assistant and add the integration in Settings > Devices & services.

## Use it

1. Open **Dashboard Studio** in the Home Assistant sidebar to design your dashboard, then click **Publish**.
2. On each tablet, open your Home Assistant address followed by `/hadash/index.html`. On Amazon Fire tablets, Fully Kiosk Browser works well. If a tablet cannot open `.local` addresses, use the IP address of Home Assistant.

Tablets pick up a new layout by themselves.

## Requirements

Home Assistant 2026.10 or later.

## Help

[Support page](https://lukaps.com/dashboardstudio/support/) or support@lukaps.com.

© 2026 ARB Consulting Inc.
