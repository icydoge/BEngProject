from __future__ import division
import os
import sys
import StringIO
from time import gmtime, strftime
from flask import Flask, send_file, abort, jsonify, request
from PIL import Image

import utils
import geocoordinate_to_location
from SAISCrawler.script import db_manager as forecast_db
from SAISCrawler.script import utils as forecast_utils
from GeoData import raster_reader, rasters, path_finder

API_LOG = os.path.abspath(os.path.join(__file__, os.pardir)) + "/api.log"
LOG_REQUESTS = True
SPATIAL_READER = raster_reader

# Main API app.
app = Flask(__name__)

# Initialise forecast database and raster reader within application context.
with app.app_context():
    forecast_dbm = forecast_db.CrawlerDB(forecast_utils.get_project_full_path() + forecast_utils.read_config('dbFile'))
    height_raster = SPATIAL_READER.RasterReader(rasters.HEIGHT_RASTER)
    aspect_raster = SPATIAL_READER.RasterReader(rasters.ASPECT_RASTER)
    contour_raster = SPATIAL_READER.RasterReader(rasters.CONTOUR_RASTER)
    static_risk_raster = SPATIAL_READER.RasterReader(rasters.RISK_RASTER)
    path_reader = path_finder.PathFinder(height_raster, aspect_raster, static_risk_raster, forecast_dbm)

@app.route('/imagery/api/v1.0/avalanche_risks/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>', methods=['GET'])
@app.route('/imagery/api/v1.0/avalanche_risks/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>/<string:forecast_date>', methods=['GET'])
def get_risk(longitude_initial, latitude_initial, longitude_final, latitude_final, forecast_date=None):
    """ Return a color-code image containing the risk of the requested coordinate and altitude area.
        Optionally lookup forecast from a defined date instead of the lastet if forecast_date is
        set. """

    not_found_message = ""

    try:

        upper_left_corner = map(float, [longitude_initial, latitude_initial])
        lower_right_corner = map(float, [longitude_final, latitude_final])
        center_coordinates = [sum(e)/len(e) for e in zip(*[upper_left_corner, lower_right_corner])]

        # Impossible geodetic coordinates.
        not_found_message = "Invalid input data."
        if (upper_left_corner[0] < -180.0) or (upper_left_corner[0] > 180.0):
            abort(400)
        if (upper_left_corner[1] < -90.0) or (upper_left_corner[1] > 90.0):
            abort(400)
        if (lower_right_corner[0] < -180.0) or (lower_right_corner[0] > 180.0):
            abort(400)
        if (lower_right_corner[1] < -90.0) or (lower_right_corner[1] > 90.0):
            abort(400)
        not_found_message = ""

        # Process static risk show param.

        if len(request.args) < 1:
            show_static_risk = '0'
        else:
            show_static_risk = request.args.get('showStaticRisk')

        if int(show_static_risk) == 1:
            show_static_risk = True
        else:
            show_static_risk = False

        # Preclude requests that are too large.
        if (abs(lower_right_corner[0] - upper_left_corner[0]) > 0.03) or (abs(lower_right_corner[1] - upper_left_corner[1]) > 0.02):
            not_found_message = "Request too large."
            abort(404)

        # Request heights and aspects from the raster, as well as static risk value from the static risk raster.
        heights_matrix = height_raster.read_points(upper_left_corner[0], upper_left_corner[1], lower_right_corner[0], lower_right_corner[1])
        aspects_matrix = aspect_raster.read_points(upper_left_corner[0], upper_left_corner[1], lower_right_corner[0], lower_right_corner[1])
        static_risk_matrix = static_risk_raster.read_points(upper_left_corner[0], upper_left_corner[1], lower_right_corner[0], lower_right_corner[1])

        # If no data returned.
        if (heights_matrix is False) or (aspects_matrix is False) or (static_risk_matrix is False):
            not_found_message = "Heights or aspects out of range."
            abort(400)
        if (len(heights_matrix) <= 0) or (len(aspects_matrix) <= 0) or (len(static_risk_matrix) <= 0):
            not_found_message = "Heights or aspects too large to request."
            abort(404)

        matrix_height = len(heights_matrix)
        matrix_width = len(heights_matrix[0])

        # Request forecast from SAIS.
        location_name = geocoordinate_to_location.get_location_name(center_coordinates[0], center_coordinates[1]).strip()
        if location_name == "":
            not_found_message = "Location name unavailable."
            abort(404)

        # Just in case multiple location ids are returned, take first one.
        location_id_list = forecast_dbm.select_location_by_name(location_name)
        if not location_id_list:
            not_found_message = "Location list empty."
            abort(404)
        location_id = int(location_id_list[0][0])

        # Look up the most recent forecasts for the location.
        defined_date_valid = False
        if (forecast_date is not None) and forecast_utils.check_date_string(forecast_date):
            location_forecasts = forecast_dbm.lookup_forecasts_by_location_id_and_date(location_id, forecast_date)
        else:
            location_forecasts = forecast_dbm.lookup_newest_forecasts_by_location_id(location_id)

        if location_forecasts == None:
            not_found_message = "Forecast for location not found."
            abort(400)
        location_forecast_list = list(location_forecasts)

        # Return forecast colours.
        location_colours = []
        for i in range(0, len(heights_matrix)):
            colour_row = []
            for j in range(0, len(heights_matrix[i])):
                colour_row.append(utils.match_aspect_altitude_to_forecast(location_forecast_list, aspects_matrix[i][j], heights_matrix[i][j]))
            location_colours.append(colour_row)

        # Build the image according to colours.
        # Create an empty image with one pixel for each point.
        return_image = Image.new("RGBA", (matrix_width, matrix_height), None)
        return_image_pixels = return_image.load()
        for i in range(return_image.size[0]):
            for j in range(return_image.size[1]):
                return_image_pixels[i,j] = utils.risk_code_to_colour(location_colours[j][i], static_risk_matrix[j][i], show_static_risk) # 2D array is in inversed order of axis.
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')

    except Exception as e:

        # Always return a result and not get held up by exception.
        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, no-data image returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        # Return an empty image.
        return_image = Image.new("RGBA", (1, 1), None)
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')


@app.route('/imagery/api/v1.0/terrain_aspects/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>', methods=['GET'])
def get_aspect(longitude_initial, latitude_initial, longitude_final, latitude_final):
    """ Return a grayscale map of terrain aspects, with 0-360 degrees mapped to 0-255 color levels. """

    not_found_message = ""

    try:

        upper_left_corner = map(float, [longitude_initial, latitude_initial])
        lower_right_corner = map(float, [longitude_final, latitude_final])
        center_coordinates = [sum(e)/len(e) for e in zip(*[upper_left_corner, lower_right_corner])]

        # Impossible geodetic coordinates.
        not_found_message = "Invalid input data."
        if (upper_left_corner[0] < -180.0) or (upper_left_corner[0] > 180.0):
            abort(400)
        if (upper_left_corner[1] < -90.0) or (upper_left_corner[1] > 90.0):
            abort(400)
        if (lower_right_corner[0] < -180.0) or (lower_right_corner[0] > 180.0):
            abort(400)
        if (lower_right_corner[1] < -90.0) or (lower_right_corner[1] > 90.0):
            abort(400)
        not_found_message = ""

        # Preclude requests that are too large.
        if (abs(lower_right_corner[0] - upper_left_corner[0]) > 0.03) or (abs(lower_right_corner[1] - upper_left_corner[1]) > 0.02):
            not_found_message = "Request too large."
            abort(404)

        # Request aspects from the raster.
        aspects_matrix = aspect_raster.read_points(upper_left_corner[0], upper_left_corner[1], lower_right_corner[0], lower_right_corner[1])
        # If no data returned.
        if (aspects_matrix is False) or (len(aspects_matrix) <= 0):
            not_found_message = "Heights or aspects out of range or too large to request."
            abort(400)

        matrix_height = len(aspects_matrix)
        matrix_width = len(aspects_matrix[0])

        # Build the image according to colours.
        # Create an empty image with one pixel for each point.
        return_image = Image.new("RGBA", (matrix_width, matrix_height), None)
        return_image_pixels = return_image.load()
        for i in range(return_image.size[0]):
            for j in range(return_image.size[1]):
                return_image_pixels[i,j] = utils.aspect_to_rbg(aspects_matrix[j][i]) # 2D array is in inversed order of axis.
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')

    except Exception as e:

        # Always return a result and not get held up by exception.
        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, no-data image returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        # Return an empty image.
        return_image = Image.new("RGBA", (1, 1), None)
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')


@app.route('/imagery/api/v1.0/contours/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>', methods=['GET'])
def get_contour(longitude_initial, latitude_initial, longitude_final, latitude_final):
    """ Return an image of terrain contours, with 50% capacity grey lines. """

    not_found_message = ""

    try:

        upper_left_corner = map(float, [longitude_initial, latitude_initial])
        lower_right_corner = map(float, [longitude_final, latitude_final])
        center_coordinates = [sum(e)/len(e) for e in zip(*[upper_left_corner, lower_right_corner])]

        # Impossible geodetic coordinates.
        not_found_message = "Invalid input data."
        if (upper_left_corner[0] < -180.0) or (upper_left_corner[0] > 180.0):
            abort(400)
        if (upper_left_corner[1] < -90.0) or (upper_left_corner[1] > 90.0):
            abort(400)
        if (lower_right_corner[0] < -180.0) or (lower_right_corner[0] > 180.0):
            abort(400)
        if (lower_right_corner[1] < -90.0) or (lower_right_corner[1] > 90.0):
            abort(400)
        not_found_message = ""

        # Preclude requests that are too large.
        if (abs(lower_right_corner[0] - upper_left_corner[0]) > 0.03) or (abs(lower_right_corner[1] - upper_left_corner[1]) > 0.02):
            not_found_message = "Request too large."
            abort(404)

        # Request contours from the raster.
        contour_matrix = contour_raster.read_points(upper_left_corner[0], upper_left_corner[1], lower_right_corner[0], lower_right_corner[1])
        # If no data returned.
        if (contour_matrix is False) or (len(contour_matrix) <= 0):
            not_found_message = "Contours out of range or too large to request."
            abort(400)

        matrix_height = len(contour_matrix)
        matrix_width = len(contour_matrix[0])

        # Build the image according to colours.
        # Create an empty image with one pixel for each point.
        return_image = Image.new("RGBA", (matrix_width, matrix_height), None)
        return_image_pixels = return_image.load()
        for i in range(return_image.size[0]):
            for j in range(return_image.size[1]):
                return_image_pixels[i,j] = utils.contour_to_rbg(contour_matrix[j][i]) # 2D array is in inversed order of axis.
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')

    except Exception as e:

        # Always return a result and not get held up by exception.
        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, no-data image returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        # Return an empty image.
        return_image = Image.new("RGBA", (1, 1), None)
        image_object = StringIO.StringIO()
        return_image.save(image_object, format="png")
        image_object.seek(0)

        return send_file(image_object, mimetype='image/png')


@app.route('/data/api/v1.0/forecast_dates/<string:longitude>/<string:latitude>', methods=['GET'])
def get_recent_forecast_dates(longitude, latitude):
    """ Return up to 50 most recent forecast dates to allow the client to request them later."""

    try:
        longitude = float(longitude)
        latitude = float(latitude)
        not_found_message = ""

        if longitude < -180 or longitude > 180 or latitude < -90 or latitude > 90:
            not_found_message = "Request out of geographical bounds."
            abort(400)

        location_name = geocoordinate_to_location.get_location_name(longitude, latitude)
        if location_name == "":
            not_found_message = "Location referenced by name unavailable."
            abort(404)
        else:
            not_found_message = "Location referenced by id unavailable." # if int() fails.
            location_id = int(forecast_dbm.select_location_by_name(location_name)[0][0])

        forecast_dates = forecast_dbm.lookup_forecast_dates(location_id)
        date_list = [date[0] for date in forecast_dates]

        return jsonify(date_list)


    except Exception as e:

        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, last 50 dates not returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        return jsonify({})

@app.route('/data/api/v1.0/find_path/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>/<string:risk_weighing>', methods=['GET'])
@app.route('/data/api/v1.0/find_path/<string:longitude_initial>/<string:latitude_initial>/<string:longitude_final>/<string:latitude_final>/<string:risk_weighing>/<string:forecast_date>', methods=['GET'])
def get_path(longitude_initial, latitude_initial, longitude_final, latitude_final, risk_weighing, forecast_date=None):
    """ Return a path (series of coordinates) found by A* search based on a weighing of risk against distance. """

    not_found_message = ""

    try:

        if (forecast_date is not None) and forecast_utils.check_date_string(forecast_date):
            custom_date = forecast_date
        else:
            custom_date = None

        risk_weighing = float(risk_weighing)
        if (risk_weighing < 0) or (risk_weighing > 1):
            not_found_message = "Invalid risk weighing."
            abort(400)

        initial = map(float, [longitude_initial, latitude_initial])
        final = map(float, [longitude_final, latitude_final])

        # Impossible geodetic coordinates.
        not_found_message = "Invalid input data."
        if (initial[0] < -180.0) or (initial[0] > 180.0):
            abort(400)
        if (initial[1] < -90.0) or (initial[1] > 90.0):
            abort(400)
        if (final[0] < -180.0) or (final[0] > 180.0):
            abort(400)
        if (final[1] < -90.0) or (final[1] > 90.0):
            abort(400)
        not_found_message = ""

        # Check request size.
        if (abs(initial[0] - final[0]) + abs(initial[1] - final[1])) > 0.5:
            not_found_message = "Request too large at API."
            abort(400)

        path, message = path_reader.find_path(initial[0], initial[1], final[0], final[1], risk_weighing, custom_date)

        if not path:
            not_found_message = "Path finding failed, probably due to excessive data size. Module message: " + message
            abort(404)

        return jsonify(path)

    except Exception as e:

        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, route not returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        return jsonify({})


@app.route('/data/api/v1.0/past_avalanches/<string:start_date>/<string:end_date>', methods=['GET'])
def get_past_avalanches(start_date, end_date):
    """ Return a list of past avalanches between start_date and end_date, with
        their datetime, locations and SAIS comments. """

    not_found_message = ""

    try:

        if forecast_dbm.convert_time_string(start_date) and forecast_dbm.convert_time_string(end_date):
            avalanches = forecast_dbm.select_past_avalanches_by_date_range(start_date, end_date)
            avalanches_data = []

            for avalanche in avalanches:

                avalanche_item = {}
                coordinates = utils.bng_to_longlat((avalanche[2], avalanche[3]))

                if not coordinates: # In case of invalid BNG values.
                    continue # Skip this.

                avalanche_item['long'] = coordinates[0]
                avalanche_item['lat'] = coordinates[1]
                avalanche_item['time'] = avalanche[4]
                avalanche_item['comment'] = avalanche[5]
                avalanche_item['height'] = height_raster.read_point(coordinates[0], coordinates[1])

                # Fix the issue when SAIS labels an avalanche outside raster boundary.
                if not avalanche_item['height']:
                    avalanche_item['height'] = 0.0;

                avalanches_data.append(avalanche_item)

        else:
            not_found_message = "Invalid date strings."
            abort(400)

        return jsonify(avalanches_data)

    except Exception as e:

        if (os.path.isfile(API_LOG)) and LOG_REQUESTS:
            with open(API_LOG, "a") as log_file:
                log_file.write(strftime("%Y-%m-%d %H:%M:%S", gmtime()) + ": error serving client, past avalanches not returned. Error: " + str(e) + ". Message: " + not_found_message + "\n")

        return jsonify({})


if __name__ == '__main__':
    app.run(debug=True)
