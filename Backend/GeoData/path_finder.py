from __future__ import division, print_function
from GeoData import rasters
from GeoData.raster_reader import RasterReader
from SAISCrawler.script import db_manager, utils
import geocoordinate_to_location
import utils as base_utils

import heapq
import numpy as np
from sys import maxsize
from math import sqrt, floor
from time import time
from datetime import datetime
from skimage.measure import block_reduce

NAISMITH_CONSTANT = 7.92
PIXEL_RES = 5 # 5 meters each direction per pixel
PIXEL_RES_DIAG = sqrt(PIXEL_RES ** 2 * 2)
MAX_BEFORE_DOWNSAMPLING = 100000
DOWNSAMPLING_TARGET = 50
MINIMUM_SEARCH_LONG = 0.008
MINIMUM_SEARCH_LAT = 0.007

class PathFinder:
    """ Class for pathfinding based on Naismith's distance,
        static risk and dynamic risk. Aspect map required
        for dynamic risk. """

    def __init__(self, height_map_reader, aspect_map_reader, static_risk_reader, dynamic_risk_cursor):

        self._height_map_reader = height_map_reader
        self._aspect_map_reader = aspect_map_reader
        self._static_risk_reader = static_risk_reader
        self._dynamic_risk_cursor = dynamic_risk_cursor
        self.__priority_queue = []


    def find_path(self, longitude_initial, latitude_initial, longitude_final, latitude_final, risk_weighing, custom_date=None):
        """ Given initial and final coordinates and a risk-to-distance weighing, find a path. """

        # Time the execution.
        start_time = time()

        # Sanity checks.
        if not all(isinstance(item, float) for item in [longitude_initial, latitude_initial, longitude_final, latitude_final]):
            return False, "Input not float."

        valid_ratio = False
        if isinstance(risk_weighing, float):
            if risk_weighing >= 0 and risk_weighing <= 1:
                valid_ratio = True

        if not valid_ratio:
            return False, "Invalid risk weighing."

        self.debug_print("Sanity check completed.")

        # Process custom date and dynamic risk.
        location_name = geocoordinate_to_location.get_location_name(longitude_initial, latitude_initial)
        location_ids = self._dynamic_risk_cursor.select_location_by_name(location_name)
        if not location_ids:
            return False, "Invalid location ID."
        location_id = int(location_ids[0][0])
        location_forecasts = self._dynamic_risk_cursor.lookup_newest_forecasts_by_location_id(location_id)

        if custom_date is not None:
            try:
                datetime.strptime(custom_date, '%Y-%m-%d')
                forecasts_of_date = self._dynamic_risk_cursor.lookup_forecasts_by_location_id_and_date(location_id, custom_date)
                if len(forecasts_of_date) > 0: # Just in case the custom date given is invalid, which happens.
                    location_forecasts = forecasts_of_date
            except ValueError:
                return False, "Invalid custom date."

        if location_forecasts is None:
            return False, "No forecast found."
        location_forecast_list = list(location_forecasts)

        original_initial = (longitude_initial, latitude_initial)
        original_final = (longitude_final, latitude_final)

        # Enlarge search window if coordinate size below minimum.
        if (abs(longitude_final - longitude_initial) < MINIMUM_SEARCH_LONG) or (abs(latitude_final - latitude_initial) < MINIMUM_SEARCH_LAT):

            if abs(longitude_final - longitude_initial) < MINIMUM_SEARCH_LONG:
                if longitude_final < longitude_initial:
                    longitude_initial, longitude_final = longitude_final, longitude_initial
                longitude_final = longitude_initial + MINIMUM_SEARCH_LONG
                self.debug_print("Grid enlarged in x direction.")

            if abs(latitude_final - latitude_initial) < MINIMUM_SEARCH_LAT:
                if latitude_final > latitude_initial:
                    latitude_initial, latitude_final = latitude_final, latitude_initial
                latitude_final = latitude_initial - MINIMUM_SEARCH_LAT
                self.debug_print("Grid enlarged in y direction.")

        # Static properties.
        height_grid = self._height_map_reader.read_points(longitude_initial, latitude_initial, longitude_final, latitude_final)

        # Immediately check how large the data is.
        x_max = len(height_grid[0]) - 1
        y_max = len(height_grid) - 1

        # Prepare the pixel resolution, since this will change if downsampling happens.
        pixel_res_x = PIXEL_RES
        pixel_res_y = PIXEL_RES

        # Process size check and downsampling calculations.
        self.debug_print("State space size: " + str(x_max) + "," + str(y_max) + ".")
        if (x_max + y_max) / 2 > MAX_BEFORE_DOWNSAMPLING:
            self.debug_print("Execution size exceeded, exiting...")
            return False, "Input too large."

        if x_max > DOWNSAMPLING_TARGET:
            downsample_x_factor = x_max // DOWNSAMPLING_TARGET + 1
            pixel_res_x = pixel_res_x * downsample_x_factor
        else:
            downsample_x_factor = 1

        if y_max > DOWNSAMPLING_TARGET:
            downsample_y_factor = y_max // DOWNSAMPLING_TARGET + 1
            pixel_res_y = pixel_res_y * downsample_y_factor
        else:
            downsample_y_factor = 1

        pixel_res_d = sqrt(pixel_res_x ** 2 + pixel_res_y ** 2)

        # More static properties
        risk_grid = self._static_risk_reader.read_points(longitude_initial, latitude_initial, longitude_final, latitude_final)
        aspect_grid = self._aspect_map_reader.read_points(longitude_initial, latitude_initial, longitude_final, latitude_final)

        if (not isinstance(height_grid, np.ndarray)) or (not isinstance(risk_grid, np.ndarray)) or (not isinstance(aspect_grid, np.ndarray)):
            return False, "Failure reading grid."

        # Downsamplings
        height_grid = block_reduce(height_grid, block_size=(downsample_y_factor, downsample_x_factor), func=np.max)
        risk_grid = block_reduce(risk_grid, block_size=(downsample_y_factor, downsample_x_factor), func=np.max)
        aspect_grid = block_reduce(aspect_grid, block_size=(downsample_y_factor, downsample_x_factor), func=np.mean)

        # Find maximum sizes again.
        x_max = len(height_grid[0]) - 1
        y_max = len(height_grid) - 1
        self.debug_print("Size after downsampling: " + str(x_max) + "," + str(y_max) + ".")

        # Find where the original coordinates are.
        initial_node = self._height_map_reader.locate_index((longitude_initial, latitude_initial), (longitude_final, latitude_final), original_initial)
        goal_node = self._height_map_reader.locate_index((longitude_initial, latitude_initial), (longitude_final, latitude_final), original_final)
        initial_node = (int(floor(initial_node[0] // downsample_x_factor)), int(floor(initial_node[1] // downsample_y_factor)))
        goal_node = (int(floor(goal_node[0] // downsample_x_factor)), int(floor(goal_node[1] // downsample_y_factor)))

        self.debug_print("Initial node: " + str(initial_node) + ", final node: " + str(goal_node))

        # Just a quick check in case we get a off-by-N in downsampling...
        initial_node = (min(initial_node[0], x_max), min(initial_node[1], y_max))
        goal_node = (min(goal_node[0], x_max), min(goal_node[1], y_max))

        # Match dynamic risk to the risk grid.
        for y in range(0, len(risk_grid)):
            for x in range(0, len(risk_grid[0])):
                risk_grid[y, x] = risk_grid[y, x] * base_utils.match_aspect_altitude_to_forecast(location_forecast_list, aspect_grid[y, x], height_grid[y, x])

        self.debug_print("Successfully loaded all data grids.")

        # Build the grid and a list of vertices.
        naismith_max = -1
        naismith_min = maxsize
        risk_grid_max = np.amax(risk_grid)
        risk_grid_min = np.amin(risk_grid)
        height_grid_max = np.amax(height_grid)
        height_grid_min = np.amin(height_grid)
        path_grid = {}

        for y in range(0, y_max + 1):
            for x in range(0, x_max + 1):
                # The grid dictionary is indexed by (displacement indices from top left), and contains a height, a 0-1
                # scaled risk, and the coordinates-indices of its neighbours, arranged with list index as followed:
                # 2 3 4
                # 5 * 6
                # 7 8 9
                # a Naismith distance is attached to each neighbour.
                height = height_grid[y, x]
                scaled_risk = (risk_grid[y, x] - risk_grid_min) / (risk_grid_max - risk_grid_min)
                risk_grid[y, x] = scaled_risk
                path_grid[(x, y)] = [height, scaled_risk]

                for j in range(y - 1, y + 2):
                    for i in range(x - 1, x + 2):

                        if ((i, j) == (x, y)):
                            continue
                        elif (0 <= i <= x_max) and (0 <= j <= y_max):
                            if (abs(i - x) + abs(j - y)) <= 1:
                                if (j - y) == 0:
                                    naismith_distance = pixel_res_x + NAISMITH_CONSTANT * max(0, height_grid[j, i] - height_grid[y, x])
                                else:
                                    naismith_distance = pixel_res_y + NAISMITH_CONSTANT * max(0, height_grid[j, i] - height_grid[y, x])
                            else:
                                naismith_distance = pixel_res_d + NAISMITH_CONSTANT * max(0, height_grid[j, i] - height_grid[y, x])

                            if naismith_distance > naismith_max:
                                naismith_max = naismith_distance
                            if naismith_distance < naismith_min:
                                naismith_min = naismith_distance
                            path_grid[(x, y)].append((i, j, naismith_distance))
                        else:
                            path_grid[(x, y)].append(None)

        # To prevent A* from getting stuck, all risk values below 5 np.percentile
        # will be changed to the 5 np.percentile value.
        non_zeros = risk_grid[risk_grid > 0]
        risk_5_percentile = np.percentile(non_zeros, 5)
        np.clip(risk_grid, risk_5_percentile, risk_grid_max, out=risk_grid)

        self.debug_print("Successfully built search grid, starting A* Search...")
        # path_grid is not yet scaled here, but risk_grid is.

        # A* Search
        self.add_to_queue(0, initial_node)
        source_index = {}
        cost_index = {}
        source_index[initial_node] = None
        cost_index[initial_node] = 0
        goal_height = height_grid[goal_node[1], goal_node[0]]

        while not self.is_queue_empty():
            current = self.pop_from_queue()
            current_node = current[1]

            if current_node == goal_node:
                break

            neighbours = [n for n in path_grid[current_node][2:] if (n is not None)]
            for neighbour in neighbours:
                neighbour_node = (neighbour[0], neighbour[1])
                # Scaling height distance values from path_grid now.
                scaled_naismith = (neighbour[2] - naismith_min) / (naismith_max - naismith_min)
                node_risk = risk_grid[neighbour[1], neighbour[0]] * risk_weighing
                edge_cost = scaled_naismith * (1 - risk_weighing) + node_risk
                new_cost = cost_index[current_node] + edge_cost
                if (neighbour_node not in cost_index) or (new_cost < cost_index[neighbour_node]):
                    cost_index[neighbour_node] = new_cost
                    prio = new_cost + self.heuristic(neighbour_node, goal_node, height_grid[neighbour[1], neighbour[0]], goal_height, naismith_max, naismith_min, pixel_res_x, pixel_res_y, pixel_res_d, node_risk)
                    self.add_to_queue(prio, neighbour_node)
                    source_index[neighbour_node] = current_node

        self.debug_print("Search completed, rebuilding path...")

        # Reconstruct the path by back-tracing.
        path = []
        current_node = goal_node
        while source_index[current_node] is not None:
            path = [current_node] + path
            current_node = source_index[current_node]
        path = [current_node] + path

        self.debug_print("Coordinate path: " + str(path) + ".")

        # Convert indices back into coordinates with height attached.
        return_path = {}
        for p in range(len(path)):
            coords = self._height_map_reader.convert_displacement_to_coordinate(longitude_initial, latitude_initial, longitude_final, latitude_final, path[p][0] * downsample_x_factor, path[p][1] * downsample_y_factor)
            way_point = {}
            way_point['long'] = str(coords[0])
            way_point['lat'] = str(coords[1])
            way_point['height'] = str(height_grid[path[p][1], path[p][0]])
            return_path[p] = way_point

        self.clean_up_queue()
        self.debug_print("Finished in " + str(time() - start_time) + " seconds.")

        return return_path, "Success."


    def add_to_queue(self, priority, coordinates):
        """ Push a coordinate and its priority onto the priority queue. """

        heapq.heappush(self.__priority_queue, (priority, coordinates))

        return True


    def pop_from_queue(self):
        """ Pop the highest priority item from the priority queue, return a tuple
            (priority, coordinates) if heap is not empty, False otherwise."""

        if len(self.__priority_queue) <= 0:
            return False

        return heapq.heappop(self.__priority_queue)


    def is_queue_empty(self):
        """ Returns True if the priority queue is empty, False otherwise. """

        if len(self.__priority_queue) <= 0:
            return True
        else:
            return False


    def clean_up_queue(self):
        """ Clear the priority queue to prepare for the next lookup. """

        self.__priority_queue = []

        return True


    def heuristic(self, current, goal, node_height, goal_height, naismith_max, naismith_min, pixel_res_x, pixel_res_y, pixel_res_d, node_risk):
        """ A diagonal heuristics function for A* search. """

        dx = abs(current[0] - goal[0])
        dy = abs(current[1] - goal[1])

        # Find the shorter edge.
        if dx < dy:
            longer_side_res = pixel_res_y
            shorter_side = dx
        else:
            longer_side_res = pixel_res_x
            shorter_side = dy

        # Heuristic = (straight section distance + diagonal section distance + Naismith distance) * immediate risk
        heuristic_distance = (\
            longer_side_res * abs(dx - dy) + \
            shorter_side * pixel_res_d + \
            NAISMITH_CONSTANT * max(0, node_height - goal_height)\
            ) * node_risk
        scaled_heuristic = (heuristic_distance - naismith_min) / (naismith_max - naismith_min)

        return scaled_heuristic


    @staticmethod
    def debug_print(message):
        if __name__ == '__main__':
            print(message)


if __name__ == '__main__':
    dbFile = utils.get_project_full_path() + utils.read_config('dbFile')
    risk_cursor = db_manager.CrawlerDB(dbFile)
    finder = PathFinder(RasterReader(rasters.HEIGHT_RASTER), RasterReader(rasters.ASPECT_RASTER), RasterReader(rasters.RISK_RASTER), risk_cursor)
    print(finder.find_path(-5.05173828125, 56.8129075187, -4.959765625, 56.7008783123, 0.5, '2017-02-20'))
    print(finder.find_path(-5.009765624999997, 56.790878312330426, -5.008765624999997, 56.79190751870019, 0.5))
    print(finder.find_path(-5.03173828125, 56.8008783123, -5.030765625, 56.8008452452, 0.5))
    print(finder.find_path(-4.99795838, 56.79702667, -4.99198645, 56.8079062, 0.5))
    print(finder.find_path(-5.0142724850797435, 56.7887604182850, -5.002987990273577, 56.80017695374134, 0.5))
    print(finder.find_path(-5.01481615318695, 56.79864610013431, -5.013048501000368, 56.80018630991712, 1.0))
