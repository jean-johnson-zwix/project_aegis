import * as cdk from 'aws-cdk-lib';
import * as iotsitewise from 'aws-cdk-lib/aws-iotsitewise';
import { Construct } from 'constructs';

/**
 * SiteWiseStack
 *
 * Defines the 4-level asset hierarchy for the project-aegis DCIM demo:
 *   Site (1) → Hall (2) → CRAC Unit (4) → [measurements/transforms/metrics on CRACUnitModel]
 *
 * Asset models use CDK L1 (CfnAssetModel) — no L2 constructs exist for SiteWise yet.
 *
 * Property alias convention (used by IoT Rule for ingestion):
 *   /sitesense/{site_id}/{hall_id}/{unit_id}/{measurement_name}
 */
export class SiteWiseStack extends cdk.Stack {
  /** Exported for IotStack to reference in outputs */
  public readonly siteId: string = 'phx-dc-01';

  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    // -------------------------------------------------------------------------
    // 1. Asset Models (bottom-up: CRAC → Hall → Site)
    // -------------------------------------------------------------------------

    const cracUnitModel = this.buildCRACUnitModel();
    const hallModel = this.buildHallModel(cracUnitModel);
    const siteModel = this.buildSiteModel(hallModel);

    // -------------------------------------------------------------------------
    // 2. Asset Instances — CRAC units
    // -------------------------------------------------------------------------

    const cracA1 = this.buildCRACAsset('CracA1', cracUnitModel, {
      unitId: 'crac-A1', hallId: 'hall-A', siteId: this.siteId,
    });
    const cracA2 = this.buildCRACAsset('CracA2', cracUnitModel, {
      unitId: 'crac-A2', hallId: 'hall-A', siteId: this.siteId,
    });
    const cracB1 = this.buildCRACAsset('CracB1', cracUnitModel, {
      unitId: 'crac-B1', hallId: 'hall-B', siteId: this.siteId,
    });
    const cracB2 = this.buildCRACAsset('CracB2', cracUnitModel, {
      unitId: 'crac-B2', hallId: 'hall-B', siteId: this.siteId,
    });

    // -------------------------------------------------------------------------
    // 3. Asset Instances — Halls (each contains 2 CRAC units)
    // -------------------------------------------------------------------------

    const hallA = new iotsitewise.CfnAsset(this, 'HallA', {
      assetModelId: hallModel.ref,
      assetName: 'Hall-A',
      assetHierarchies: [
        { logicalId: 'cracs', childAssetId: cracA1.ref },
        { logicalId: 'cracs', childAssetId: cracA2.ref },
      ],
    });

    const hallB = new iotsitewise.CfnAsset(this, 'HallB', {
      assetModelId: hallModel.ref,
      assetName: 'Hall-B',
      assetHierarchies: [
        { logicalId: 'cracs', childAssetId: cracB1.ref },
        { logicalId: 'cracs', childAssetId: cracB2.ref },
      ],
    });

    // -------------------------------------------------------------------------
    // 4. Asset Instance — Site (contains both halls)
    // -------------------------------------------------------------------------

    new iotsitewise.CfnAsset(this, 'SitePhxDc01', {
      assetModelId: siteModel.ref,
      assetName: 'phx-dc-01',
      assetHierarchies: [
        { logicalId: 'halls', childAssetId: hallA.ref },
        { logicalId: 'halls', childAssetId: hallB.ref },
      ],
    });

    // -------------------------------------------------------------------------
    // 5. Stack outputs
    // -------------------------------------------------------------------------

    new cdk.CfnOutput(this, 'CRACUnitModelId', {
      value: cracUnitModel.ref,
      exportName: 'ProjectAegis-CRACUnitModelId',
      description: 'SiteWise CRACUnitModel ID',
    });
    new cdk.CfnOutput(this, 'CracA1AssetId', {
      value: cracA1.ref,
      exportName: 'ProjectAegis-CracA1AssetId',
    });
    new cdk.CfnOutput(this, 'CracA2AssetId', {
      value: cracA2.ref,
      exportName: 'ProjectAegis-CracA2AssetId',
    });
    new cdk.CfnOutput(this, 'CracB1AssetId', {
      value: cracB1.ref,
      exportName: 'ProjectAegis-CracB1AssetId',
    });
    new cdk.CfnOutput(this, 'CracB2AssetId', {
      value: cracB2.ref,
      exportName: 'ProjectAegis-CracB2AssetId',
    });
  }

  // -------------------------------------------------------------------------
  // Model builders
  // -------------------------------------------------------------------------

  private buildCRACUnitModel(): iotsitewise.CfnAssetModel {
    return new iotsitewise.CfnAssetModel(this, 'CRACUnitModel', {
      assetModelName: 'CRACUnitModel',
      assetModelDescription: 'CRAC unit — attributes, measurements (Input Registers), transforms, and metrics',
      assetModelProperties: [

        // --- Attributes (static metadata) ---
        { logicalId: 'unit_id',        name: 'unit_id',        dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'unknown' } } },
        { logicalId: 'manufacturer',   name: 'manufacturer',   dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'unknown' } } },
        { logicalId: 'model_number',   name: 'model_number',   dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'unknown' } } },
        { logicalId: 'install_date',   name: 'install_date',   dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: '1970-01-01' } } },
        { logicalId: 'max_cooling_kw', name: 'max_cooling_kw', dataType: 'DOUBLE', type: { typeName: 'Attribute', attribute: { defaultValue: '60.0' } } },

        // --- Measurements (Input Registers IR 30001–30005 — read-only sensors) ---
        {
          logicalId: 'supply_temp_c', name: 'supply_temp_c', dataType: 'DOUBLE', unit: '°C',
          type: { typeName: 'Measurement' },
        },
        {
          logicalId: 'return_temp_c', name: 'return_temp_c', dataType: 'DOUBLE', unit: '°C',
          type: { typeName: 'Measurement' },
        },
        {
          logicalId: 'supply_humidity_pct', name: 'supply_humidity_pct', dataType: 'DOUBLE', unit: '%',
          type: { typeName: 'Measurement' },
        },
        {
          logicalId: 'fan_rpm', name: 'fan_rpm', dataType: 'DOUBLE', unit: 'RPM',
          type: { typeName: 'Measurement' },
        },
        {
          logicalId: 'power_draw_kw', name: 'power_draw_kw', dataType: 'DOUBLE', unit: 'kW',
          type: { typeName: 'Measurement' },
        },

        // --- Transforms (computed from measurements, no time window) ---
        {
          // delta_t_c = return_temp_c - supply_temp_c
          // Key KPI: positive delta means the unit is transferring heat from the room.
          // If delta_t_c < 0, the validator.py drops the reading as physically impossible.
          logicalId: 'delta_t_c', name: 'delta_t_c', dataType: 'DOUBLE', unit: '°C',
          type: {
            typeName: 'Transform',
            transform: {
              expression: 'r - s',
              variables: [
                { name: 'r', value: { propertyLogicalId: 'return_temp_c' } },
                { name: 's', value: { propertyLogicalId: 'supply_temp_c' } },
              ],
            },
          },
        },
        {
          // cooling_efficiency = delta_t_c / power_draw_kw  (°C per kW)
          // Higher is better. Degradation alarm fires when this falls below 70% of historical avg.
          logicalId: 'cooling_efficiency', name: 'cooling_efficiency', dataType: 'DOUBLE', unit: '°C/kW',
          type: {
            typeName: 'Transform',
            transform: {
              expression: 'd / p',
              variables: [
                { name: 'd', value: { propertyLogicalId: 'delta_t_c' } },
                { name: 'p', value: { propertyLogicalId: 'power_draw_kw' } },
              ],
            },
          },
        },

        // --- Metrics (aggregated over tumbling time windows) ---
        {
          // 5-minute average power draw — used for short-term load trending
          logicalId: 'avg_power_5min', name: 'avg_power_5min', dataType: 'DOUBLE', unit: 'kW',
          type: {
            typeName: 'Metric',
            metric: {
              expression: 'avg(p)',
              variables: [{ name: 'p', value: { propertyLogicalId: 'power_draw_kw' } }],
              window: { tumbling: { interval: '5m' } },
            },
          },
        },
        {
          // max_supply_temp_1h — 1-hour rolling maximum supply temperature.
          // Used by the HighSupplyTemp alarm: max_supply_temp_1h > target_supply_temp_c + 3.
          // SiteWise metrics support: avg, sum, count, min, max, first, last only.
          // max() is a conservative substitute for P95 — a single spike triggers the alarm,
          // which is acceptable for CRAC units where sustained high temp causes hardware damage.
          logicalId: 'max_supply_temp_1h', name: 'max_supply_temp_1h', dataType: 'DOUBLE', unit: '°C',
          type: {
            typeName: 'Metric',
            metric: {
              expression: 'max(t)',
              variables: [{ name: 't', value: { propertyLogicalId: 'supply_temp_c' } }],
              window: { tumbling: { interval: '1h' } },
            },
          },
        },
        {
          // Daily energy consumption (kWh approx) — assumes 1-min publish cadence
          // sum(kW) over 1440 samples × (1/60 h per sample) = kWh
          logicalId: 'total_kwh_1d', name: 'total_kwh_1d', dataType: 'DOUBLE', unit: 'kWh',
          type: {
            typeName: 'Metric',
            metric: {
              expression: 'sum(p) / 60',
              variables: [{ name: 'p', value: { propertyLogicalId: 'power_draw_kw' } }],
              window: { tumbling: { interval: '1d' } },
            },
          },
        },
      ],
    });
  }

  private buildHallModel(cracModel: iotsitewise.CfnAssetModel): iotsitewise.CfnAssetModel {
    return new iotsitewise.CfnAssetModel(this, 'HallModel', {
      assetModelName: 'HallModel',
      assetModelDescription: 'Data hall — contains CRAC units',
      assetModelProperties: [
        { logicalId: 'hall_id',              name: 'hall_id',              dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'unknown' } } },
        { logicalId: 'design_capacity_kw',   name: 'design_capacity_kw',   dataType: 'DOUBLE', type: { typeName: 'Attribute', attribute: { defaultValue: '200.0' } } },
        {
          logicalId: 'target_supply_temp_c',
          name: 'target_supply_temp_c',
          dataType: 'DOUBLE',
          unit: '°C',
          // HighSupplyTemp alarm: p95_supply_temp_1h > target_supply_temp_c + 3
          type: { typeName: 'Attribute', attribute: { defaultValue: '18.0' } },
        },
      ],
      assetModelHierarchies: [
        {
          logicalId: 'cracs',
          name: 'CRAC Units',
          childAssetModelId: cracModel.ref,
        },
      ],
    });
  }

  private buildSiteModel(hallModel: iotsitewise.CfnAssetModel): iotsitewise.CfnAssetModel {
    return new iotsitewise.CfnAssetModel(this, 'SiteModel', {
      assetModelName: 'SiteModel',
      assetModelDescription: 'Data centre site — top of the 4-level hierarchy',
      assetModelProperties: [
        { logicalId: 'site_id',      name: 'site_id',      dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'phx-dc-01' } } },
        { logicalId: 'region',       name: 'region',       dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'us-west-2' } } },
        { logicalId: 'climate_zone', name: 'climate_zone', dataType: 'STRING', type: { typeName: 'Attribute', attribute: { defaultValue: 'hot-dry' } } },
      ],
      assetModelHierarchies: [
        {
          logicalId: 'halls',
          name: 'Data Halls',
          childAssetModelId: hallModel.ref,
        },
      ],
    });
  }

  // -------------------------------------------------------------------------
  // Asset builder — creates one CRAC unit asset with property aliases
  // -------------------------------------------------------------------------

  private buildCRACAsset(
    id: string,
    model: iotsitewise.CfnAssetModel,
    opts: { unitId: string; hallId: string; siteId: string },
  ): iotsitewise.CfnAsset {
    const { unitId, hallId, siteId } = opts;
    const aliasPrefix = `/sitesense/${siteId}/${hallId}/${unitId}`;

    // Property aliases are set on measurements only (transforms and metrics are derived).
    // The IoT Rule uses these aliases with substitution templates to route MQTT → SiteWise.
    return new iotsitewise.CfnAsset(this, id, {
      assetModelId: model.ref,
      assetName: unitId,
      assetProperties: [
        { logicalId: 'supply_temp_c',       alias: `${aliasPrefix}/supply_temp_c`,       notificationState: 'DISABLED' },
        { logicalId: 'return_temp_c',       alias: `${aliasPrefix}/return_temp_c`,       notificationState: 'DISABLED' },
        { logicalId: 'supply_humidity_pct', alias: `${aliasPrefix}/supply_humidity_pct`, notificationState: 'DISABLED' },
        { logicalId: 'fan_rpm',             alias: `${aliasPrefix}/fan_rpm`,             notificationState: 'DISABLED' },
        { logicalId: 'power_draw_kw',       alias: `${aliasPrefix}/power_draw_kw`,       notificationState: 'DISABLED' },
      ],
    });
  }
}
