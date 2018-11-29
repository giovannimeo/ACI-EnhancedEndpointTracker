import {Injectable} from '@angular/core';
import {FabricSettings} from '../_model/fabric-settings';
import {Fabric} from '../_model/fabric';

@Injectable({
    providedIn: 'root'
})

export class PreferencesService {
    pageSize = 10;
    endpointDetailsObject: any;
    selectedEndpoint = {};
    fabricSettings: FabricSettings;
    fabric: Fabric;
    checkedThreadStatus = false;

    constructor() {
        this.fabricSettings = new FabricSettings();
        this.fabric = new Fabric();
    }
}
