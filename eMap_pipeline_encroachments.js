const axios = require('axios');
const fs = require('fs');
const baseURL = 'https://gis.dep.pa.gov/depadv/rest/services/eMapPA/eMapPA/MapServer';

const searchTerms = ["ENCROACHMENT"];

function toCsv(data) {
  if (!data.length) return '';

  const keysSet = new Set();
  data.forEach(item => {
    Object.keys(item).forEach(k => keysSet.add(k));
  });
  const keys = Array.from(keysSet);

  const escapeCsv = val => {
    if (val == null) return '';
    const str = String(val).replace(/"/g, '""');
    return `"${str}"`;
  };

  const header = keys.join(',');
  const rows = data.map(row => keys.map(k => escapeCsv(row[k])).join(','));

  return [header, ...rows].join('\n');
}

(async () => {
  const results = [];

  // Get all sublayers of group layer 208
  const { data: groupInfo } = await axios.get(`${baseURL}/208?f=json`);
  const sublayerIds = groupInfo.subLayers?.map(l => l.id) || [];

  console.log(`Found ${sublayerIds.length} sublayers.`);

  for (const layerId of sublayerIds) {
    const layerURL = `${baseURL}/${layerId}`;
    try {
      const { data: layerInfo } = await axios.get(`${layerURL}?f=json`);

      if (!layerInfo || layerInfo.type !== "Feature Layer" || !layerInfo.fields) {
        console.log(`Skipping layer ${layerId}: Not a feature layer.`);
        continue;
      }

      const fieldNames = layerInfo.fields.map(f => f.name.toUpperCase());
      if (!fieldNames.includes("PRIMARY_FACILITY_TYPE")) {
        console.log(`Layer ${layerId} missing PRIMARY_FACILITY_TYPE.`);
        continue;
      }

      // Construct WHERE clause for multiple terms (OR)
      const where = searchTerms
        .map(term => `UPPER(PRIMARY_FACILITY_TYPE) LIKE '%${term.toUpperCase()}%'`)
        .join(' OR ');

      const pageSize = 1000;
      let offset = 0;
      let totalForLayer = 0;

      while (true) {
        const { data: queryResult } = await axios.get(`${layerURL}/query`, {
          params: {
            where,
            outFields: "*",
            returnGeometry: true,
            f: "json",
            resultOffset: offset,
            resultRecordCount: pageSize,
          }
        });

        const features = queryResult.features || [];
        if (features.length === 0) break;

        features.forEach(f => {
          results.push({
            layerId,
            ...f.attributes,
            geometry_x: f.geometry?.x,
            geometry_y: f.geometry?.y,
            geometry_z: f.geometry?.z,
          });
        });

        offset += features.length;
        totalForLayer += features.length;

        console.log(`Layer ${layerId}: Fetched ${totalForLayer} records so far...`);
      }

      console.log(`Layer ${layerId}: Total ${totalForLayer} records fetched.`);

    } catch (err) {
      console.warn(`Error processing layer ${layerId}: ${err.message}`);
    }
  }

  const csv = toCsv(results);
  fs.writeFileSync('OG_ESCGP_geolocations.csv', csv);
  console.log(`Finished. Saved ${results.length} records to OG_ESCGP_geolocations.csv`);
})();
